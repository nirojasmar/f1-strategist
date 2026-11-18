# Motor de estrategia: modelo, fórmulas y lectura de una evaluación

Documento de referencia de la Fase 4. Describe qué calcula el motor de
estrategia, con qué fórmulas, qué supuestos acepta, qué riesgos declara y cómo
interpretar una evaluación guardada. No sustituye al plan
[`plans/plan_fase_4_motor_de_estrategia.md`](../plans/plan_fase_4_motor_de_estrategia.md),
que fija el alcance y los criterios de salida, ni al
[outline técnico](outline_tecnico_ingeniero_carrera_f1_25.md), que ubica el
módulo en la arquitectura.

## 1. Alcance

El motor responde una sola pregunta: **con los datos disponibles en esta
vuelta, ¿conviene mantener el compuesto montado o parar?** Devuelve siempre una
evaluación estructurada, nunca una frase generada. Las cuatro acciones
publicadas son:

| Acción | Significado |
| --- | --- |
| `NO_CHANGE` | Ninguna parada supera el umbral de ganancia; la referencia es continuar. |
| `PIT_NOW` | La mejor alternativa es parar al final de esta vuelta. |
| `PIT_IN` | La mejor alternativa es parar dentro de `N` vueltas. |
| `INSUFFICIENT_DATA` | Faltan entradas críticas o la comparación no es defendible. |

Principios que condicionan todo lo demás:

- El motor es **puro y determinista**: no lee reloj, no hace E/S y no depende
  del orden de llegada de los paquetes más allá de lo que ya resume el estado.
- **Un dato ausente se declara, no se estima en silencio.** Si falta, el campo
  queda en `None` y la calidad baja a `insufficient` o `unavailable`.
- El objetivo es el **tiempo total de carrera**. La posición de reincorporación
  se usa como riesgo y como contexto, no como función objetivo (decisión D1).
- El español se resuelve en la capa de presentación: el motor emite claves de
  mensaje en `snake_case` inglés.

## 2. Flujo de una evaluación

`evaluate_once` (`src/f1_strategist/strategy/engine.py`) ejecuta siempre la
misma secuencia:

1. `build_compound_models`: ajusta el ritmo y el desgaste de cada compuesto
   candidato a partir del histórico de vueltas limpias y del perfil del fin de
   semana.
2. `estimate_pit_loss`: obtiene la pérdida en boxes con la primera fuente
   disponible y conserva su origen, calidad y banda.
3. `estimate_rejoin`: si hay pérdida, estima la posición de reincorporación con
   los huecos conocidos.
4. `tyre_options`: enumera los juegos disponibles y el límite de vida de cada
   uno.
5. `_unmodeled_conditions`: detecta condiciones que el MVP no modela (lluvia,
   safety car, bandera roja, daños) y las publica como riesgos.
6. `_data_quality`: resume la disponibilidad de ritmo, pérdida, vueltas
   restantes, antigüedad y muestras.
7. `decide`: evalúa alternativas bajo nueve escenarios de perturbación y elige
   la recomendación.
8. Se publica una `StrategyEvaluation` con huellas, alternativas ordenadas,
   supuestos, riesgos y sensibilidades.

Todas las entradas del cálculo están en `MetricInputs`: instantánea de estado,
histórico de vueltas, configuración de métricas, instante de observación,
índice de coche y perfil del fin de semana.

## 3. Modelo de ritmo por compuesto

### 3.1 Ajuste lineal sobre vueltas limpias

Para cada compuesto se toman sus vueltas limpias de la tanda actual y se ajusta
por mínimos cuadrados:

```text
tiempo(vuelta) = base_s + slope_s_por_vuelta · edad_vuelta
```

- Se exigen **al menos 3 vueltas limpias**; con menos no hay ajuste y el
  compuesto queda sin modelo propio.
- Una vuelta deja de ser limpia si está marcada como inválida, si es de
  entrada o salida de boxes, si hay bandera amarilla, coche de seguridad o
  condiciones de lluvia, o si el compuesto no coincide.
- `quality` se degrada según el número de muestras (`ok` / `low` / `stale`) y
  por la antigüedad de la última vuelta utilizable.

### 3.2 Mezcla con el prior del fin de semana

El ajuste no se usa en solitario. Se mezcla con el prior derivado de las
sesiones anteriores del mismo fin de semana:

```text
estimación = (k · prior + n · carrera) / (k + n)
```

donde `n` son las vueltas propias válidas y `k` es `prior_strength_k`
(por defecto `3.0`). Con el valor por defecto la carrera pesa `n/(n+3)` y el
prior `3/(n+3)`; sin vueltas de carrera se usa el prior y, sin prior, la
estimación de la carrera. La función vive en `weekend_profile.blend_with_prior`
y se aplica a la pendiente, a la base alineada y a la tasa de desgaste.

Antes de mezclar, la base del prior se **alinea** al ritmo de referencia del
compuesto montado (`aligned_prior_base`); así el prior aporta forma (pendiente)
sin arrastrar un nivel de ritmo de otra sesión. Si la mezcla se aplica, la
evaluación declara `profile_prior_used` en los supuestos y la calidad resultante
es la peor de las dos fuentes.

### 3.3 Sin ajuste suficiente

Si el compuesto no tiene 3 vueltas limpias pero el perfil tiene pendiente, se
usa la pendiente del perfil escalada por la diferencia de compuesto; si tampoco
hay perfil, la pendiente del compuesto de referencia
(`slope_from_reference`). En ambos casos la calidad baja y la nota
correspondiente aparece en el modelo.

### 3.4 Desgaste

`wear_rate_pct_per_lap` se ajusta igual que el ritmo (regresión sobre el
desgaste al cierre de cada vuelta limpia) y se mezcla con el prior de desgaste
con la misma fórmula. Si hay mezcla se añade la nota `wear_blended`.

### 3.5 Vida útil del neumático

Conviven dos límites que **no están en las mismas unidades** y por eso se
normalizan siempre a **edad total del neumático**:

| Fuente | Unidad original | Conversión |
| --- | --- | --- |
| `usableLife` del juego | edad máxima recomendada | se usa tal cual |
| `lifeSpan` del juego | vueltas **restantes** desde la edad actual | `edad_actual + lifeSpan` |
| Límite de desgaste (`tyre_wear_limit_pct`) | desgaste restante / tasa | `edad_actual + (límite - desgaste_actual) / tasa` |

El límite efectivo es el mínimo de los candidatos disponibles. Si no hay ningún
candidato, el campo queda en `None`: significa «sin límite conocido», no
«ilimitado», y se declara en la evaluación.

Los juegos del inventario siguen el mismo criterio en
`strategy/tyre_options.py`: `useful_life_laps` está expresado en **vueltas
restantes**, se escala por el desgaste ya consumido (`1 - desgaste/100`) y se
recorta con el `lifeSpan` del juego. Al proyectar una parada se convierte a edad
total sumando la edad inicial del neumático.

### 3.6 Degradación después del límite (cliff)

Si `cliff_penalty_s_per_lap` está configurado, cada vuelta proyectada por
encima de la vida útil añade esa penalización por vuelta:

```text
tiempo(edad) = base + slope · edad
             + cliff · max(0, edad − vida_útil)
```

Sin parámetro no se inventa una caída de rendimiento: el límite solo marca
viabilidad y riesgo.

## 4. Proyección de tiempos

`project_lap_times(model, start_age, laps)` devuelve el tiempo de cada vuelta
futura aplicando el modelo anterior con edades `start_age`, `start_age + 1`, …
hasta cubrir `laps` vueltas.

`_projected_time(model, start_age, laps, initial_fraction)` integra esa
proyección cubriendo exactamente `laps` vueltas, incluidas las fracciones:

- La fracción que queda de la vuelta actual (`initial_fraction`) se proyecta
  con la edad actual.
- Las vueltas completas se proyectan con edades crecientes.
- La fracción final se proyecta proporcionalmente.

Esto es lo que permite comparar alternativas a mitad de vuelta sin redondear a
vueltas completas ni contar dos veces la vuelta en curso.

## 5. Alternativas y tiempo restante

Se evalúan tres familias de alternativas:

- **`stay_out`** (`BASELINE_STAY_OUT`): continuar con el compuesto montado.
  Solo existe si la regla de compuestos lo permite y si el neumático llega al
  final (`edad_actual + vueltas_restantes <= vida_útil`).
- **`pit_now`**: parar al final de esta vuelta, con cada compuesto elegible del
  inventario.
- **`pit_in_N`**: parar dentro de `N` vueltas, con `N` desde 1 hasta el final de
  la ventana (`max_pit_in_laps`) y cada compuesto elegible.

La regla de los dos compuestos secos se deduce de los stints declarados por el
juego, conservando el compuesto que la telemetría observó vuelta a vuelta si el
juego reescribe un stint abierto (por ejemplo, tras una bandera roja). Si el
histórico no cubre todas las vueltas con compuesto conocido, o falta el visual
de un compuesto usado del que depende acreditar el medio o duro, la regla queda
como no acreditada (`two_compounds_unverified`): no se declara parada
obligatoria ni se afirma que la regla esté cumplida.

El tiempo restante de una alternativa es:

```text
restante = Σ vueltas proyectadas antes de la parada
         + Σ vueltas proyectadas con el neumático nuevo
         + pérdida_en_boxes
         + coste_de_tráfico (solo si está configurado y la parada es inmediata)
```

La pérdida en boxes **ya incluye la vuelta de entrada y la de salida**; no se
añade ninguna penalización adicional por salir de boxes. La ganancia publicada
es la diferencia contra la línea base:

```text
ganancia = restante(línea base) − restante(alternativa)
```

Los positivos favorecen la parada; los negativos favorecen mantener. El orden de
publicación es determinista: línea base, `pit_now`, `pit_in_1`, `pit_in_2`, … y,
dentro de cada grupo, el orden de las opciones de neumático.

## 6. Pérdida en boxes

`estimate_pit_loss` prueba las fuentes en este orden y se queda con la primera
disponible:

| Orden | Fuente | Cómo se obtiene |
| --- | --- | --- |
| 1 | `override` | Valor explícito de configuración (`pit_loss_override_s`). |
| 2 | `field` | Pares entrada/salida de la sesión, comparados con la mediana de vueltas limpias alrededor; dispersión por MAD. |
| 3 | `profile` | Prior del fin de semana medido en sesiones anteriores. |
| 4 | `lane` | Tiempo de carril de una vuelta de boxes **completa** (la vuelta de salida ya cerrada). |
| 5 | `circuit` | Prior estático por circuito del CSV `F1-pit-loss-calendars-2025-2026.csv`. |
| — | `none` | Sin fuente: `value_s=None`, calidad `unavailable` y nota `no_pit_loss_source`. |

Reglas que la implementación respeta:

- Una muestra **parcial** de carril (por ejemplo, una vuelta de entrada cerrada
  con 1,933 s) no es una pérdida completa y se descarta. Solo se acepta la
  vuelta que ya ha salido del carril.
- El prior por circuito se resuelve por nombre de circuito con alias explícitos;
  no se sustituye por un circuito parecido. Cada valor conserva su origen, su
  calidad y una banda, y los valores no oficiales se marcan como tales.
- Con coche de seguridad o VSC el valor del calendario es una estimación
  identificada (`circuit_vsc_pit_loss_estimate`,
  `circuit_sc_pit_loss_estimate`) y la incertidumbre de SC solo se publica si la
  fuente la indica.

## 7. Reincorporación y tráfico

`estimate_rejoin` estima la posición de salida a partir de la pérdida en boxes
y de los huecos conocidos:

- Requiere la pérdida y huecos suficientes; si faltan, no hay posición y la
  evaluación lo declara (`rival_gap_unavailable`, `leader_gap_unavailable`).
- Supone que los rivales no paran (`rivals_do_not_pit`): es una aproximación,
  no una predicción de la carrera real.
- Las vueltas de doblados se ignoran (`lapped_cars_ignored`).
- Se publica un **rango** (`rejoin_position_range`) derivado de la banda de
  pérdida y de la resolución de los huecos, junto con su calidad.

El coste de tráfico solo se suma al tiempo cuando
`traffic_cost_s_per_lap` está configurado y la parada es inmediata. Sin
parámetro, el tráfico es un **riesgo declarado** (`traffic_not_priced`) y nunca
un tiempo inventado. Si se aplica, se añade `traffic_cost_applied`.

La posición de reincorporación se presenta siempre etiquetada: es la de la
**recomendación** o la de una **alternativa** que no se recomienda. Nunca se
publica una reincorporación de alternativa como si fuera una recomendación.

## 8. Decisión, incertidumbre y confianza

La decisión no compara un único par de números: repite la evaluación bajo
**nueve escenarios** y exige que la ventaja sobreviva a la incertidumbre.

1. `neutral`.
2. Pendiente de degradación arriba y abajo (`wear_slope_band_pct`, por defecto
   ±15 %).
3. Pérdida en boxes arriba y abajo (`pit_loss_band_s`, por defecto ±2 s).
4. Las cuatro combinaciones de ambas esquinas.

De cada escenario se obtiene el tiempo de cada alternativa; el intervalo de
ganancia publica el mínimo y el máximo. La regla es:

```text
se recomienda parar  ⇔  límite_inferior(ganancia) > min_gain_s
```

Con `min_gain_s = 1.0` por defecto. Si ninguna parada cumple, la respuesta es
`NO_CHANGE`: es una respuesta válida, no un fallo. La confianza combina la
calidad de las entradas, el número de muestras de ritmo, el peso del prior, el
ancho del intervalo respecto a `min_gain_s` y una penalización si los datos son
obsoletos. Se acota a `[0, 1]` y se publica junto a la recomendación.

`decide` declara además:

- **`sensitive_to`**: `wear_slope` o `pit_loss_s` cuando la acción o la
  alternativa elegida cambian al perturbar ese parámetro.
- **`mandatory_stop`**: la regla de los dos compuestos secos, acreditada con el
  histórico, impide mantener; se publica el plan de referencia, no una
  recomendación libre.
- **`alternative_unstable`** / **`alternative_quality_insufficient`**: las
  alternativas existen pero no son defendibles; cada motivo se publica solo si
  se verifica su causa.

## 9. Estados, motivos y honestidad

`INSUFFICIENT_DATA` no es un error: es la respuesta cuando falta algo crítico.
Cada motivo se publica como clave y se traduce en la presentación:

| Clave | Cuándo aparece |
| --- | --- |
| `session_is_not_a_race` | La sesión activa no es una carrera. |
| `laps_remaining_unavailable` | No se conocen las vueltas restantes (o son cero). |
| `pace_quality_insufficient` | El ritmo del compuesto relevante no es utilizable. |
| `pit_loss_unavailable` | No hay ninguna fuente de pérdida en boxes. |
| `data_stale` | El histórico está fuera de la ventana de frescura. |
| `stay_out_unavailable` | No se pudo evaluar continuar con el juego montado. |
| `unmodeled_condition` | Hay una condición que el MVP no modela. |
| `decision_without_alternatives` | No se pudo construir ninguna alternativa. |

Condiciones no modeladas que suspenden la recomendación: `wet_conditions`,
`safety_car_conditions`, `red_flag_conditions`, `damage_not_modelled` y
`tyre_damage_not_modelled`. El motor no inventa una estrategia bajo safety car
ni bajo lluvia: lo dice.

## 10. Cómo leer una evaluación

Una `StrategyEvaluation` guardada contiene, en este orden de lectura:

1. **`lap` y `laps_remaining`**: contexto temporal. Si `laps_remaining` es
   `None`, cualquier proyección es incompleta.
2. **`recommendation`**: `action`, `key`, `pit_lap`, `compound`, `set_index`,
   `gain_vs_baseline_s`, `gain_interval_s`, `confidence` y `notes`. En
   `NO_CHANGE` e `INSUFFICIENT_DATA` los campos de parada quedan vacíos porque
   **no aplican a la acción**, no porque falten datos.
3. **`alternatives`**: todas las alternativas evaluadas, ordenadas por clave,
   con su tiempo restante, ganancia, posición de reincorporación, riesgo de
   tráfico y calidad. Incluye la línea base cuando existe.
4. **`assumptions`**: `fuel_effect_not_separated`, `rivals_do_not_pit`,
   `profile_prior_used`. Son límites del cálculo, no errores.
5. **`risks`**: `traffic_not_priced`, `tyre_life_exceeded`,
   `wet_conditions`, `safety_car_conditions`, `red_flag_conditions`,
   `damage_not_modelled`, `tyre_damage_not_modelled`.
6. **`sensitive_to`**: parámetros cuya variación cambia la recomendación.
7. **`data_quality`** y **`pit_loss_quality`**: calidad global y de la pérdida.
8. **`inputs_hash`** y **`profile_hash`**: huellas de las entradas y del perfil.
   Permiten comprobar que dos evaluaciones parten de los mismos datos. La
   huella no incluye relojes monotónicos, pero sí qué grupos de paquetes están
   obsoletos en el instante de evaluación y las configuraciones efectivas.
9. **`projected_lap_times_s`**: proyección por vuelta del compuesto montado,
   para el coche y compuesto indicados en `forecast_car_index` /
   `forecast_compound`.
10. **`pit_loss_s`**, **`pit_loss_band_s`**, **`pit_loss_source`**,
    **`pit_loss_notes`**: valor, banda, origen y matices de la pérdida.
11. **`rejoin_position_range`** y **`rejoin_quality`**: horquilla de
    reincorporación y su calidad.

En el panel de la consola, los campos se presentan con tres estados distintos y
explícitos: **`Desconocido`** (el dato no está disponible), **`No aplica`** (la
acción no usa ese campo) y **el valor calculado**. Cuando la acción no es una
parada pero existe una parada evaluada, se muestra aparte como «Mejor parada
calculada», identificada como alternativa sin recomendación.

La trazabilidad se completa con `strategy show` (una evaluación guardada) y
`strategy review` (recorrido de todas las evaluaciones de una sesión).

## 11. Parámetros configurables

Todos viven en `StrategyConfig` (`src/f1_strategist/config.py`) y se pueden
sobrescribir por circuito con `tracks.<circuito>`:

| Parámetro | Defecto | Efecto |
| --- | --- | --- |
| `enabled` | `true` | Activa el motor. |
| `evaluation_min_interval_s` | `1.0` | Cadencia mínima de evaluación en vivo. |
| `min_gain_s` | `1.0` | Ganancia mínima exigida al límite inferior. |
| `wear_slope_band_pct` | `15.0` | Amplitud de la perturbación de pendiente. |
| `pit_loss_band_s` | `2.0` | Banda de la pérdida y amplitud de su perturbación. |
| `prior_strength_k` | `3.0` | Peso del prior frente a la carrera. |
| `max_pit_in_laps` | `10` | Ventana máxima de `PIT_IN`. |
| `require_two_dry_compounds` | `true` | Regla de los dos compuestos secos (D2). |
| `traffic_cost_s_per_lap` | `null` | Coste de tráfico; `null` lo deja como riesgo. |
| `cliff_penalty_s_per_lap` | `null` | Penalización por vuelta tras la vida útil. |
| `pit_loss_override_s` | `null` | Pérdida forzada. |

Los valores marcados como provisionales se calibrarán en la Fase 9. Los campos
sin valor universal quedan en `null` a propósito: es preferible declarar la
ausencia a fijar un número arbitrario.

## 12. Supuestos y limitaciones conocidas

Supuestos que la evaluación declara:

- El efecto del combustible no se separa del ritmo (`fuel_effect_not_separated`):
  la pendiente absorbe parte del aligeramiento del coche.
- Los rivales no paran (`rivals_do_not_pit`): la reincorporación es estática.
- El prior del fin de semana se usa cuando existe (`profile_prior_used`) y su
  nivel se alinea al ritmo de referencia.

Limitaciones del MVP, fuera del alcance de la Fase 4:

- **Safety car y bandera roja** no se modelan: suspenden la recomendación.
- **Lluvia** no se modela con compuestos de mojado: se declara
  `wet_conditions`.
- **Daños** de coche y de neumático no se modelan.
- La **posición de reincorporación** depende de los huecos conocidos en vivo y
  del supuesto de que los rivales no paran; el prior externo por circuito
  (S13) mejora la pérdida, no la posición.
- La **calibración** de umbrales y bandas queda para la Fase 9.

Incidencias medidas en la validación de extremo a extremo (S10), con su estado
posterior:

- **S10-R1, rendimiento.** Con los doce juegos del escenario de referencia, una
  evaluación tarda del orden de 13 ms de media (p99 ≈ 16 ms) y crece de forma
  lineal con el número de alternativas. El criterio de la sección 11 del plan
  (media < 1 ms, p99 < 5 ms) no se cumple; el coste es admisible para la
  cadencia de evaluación en vivo, pero debe optimizarse antes de ampliar la
  ventana de alternativas.
- **S10-H1, huella entre velocidades de reproducción.** Corregida en la fase 5
  (tareas H1 y H2): `inputs_hash` excluye los relojes monotónicos y el instante
  de evaluación, e incluye la clasificación de obsolescencia de los grupos de
  paquetes cuando hay alguno obsoleto. Dos entradas idénticas salvo por un grupo
  obsoleto ya no comparten huella, y con todo fresco la huella coincide con la
  de H1. `play` y `replay` conservan el tiempo de la captura a cualquier
  velocidad, así que la huella no depende de las esperas. El runner offline
  expone `clock_scale` (rechaza cero): comprime el reloj completo, pero los
  umbrales de obsolescencia están en segundos absolutos, de modo que comprimirlo
  puede cambiar la frescura y la huella.

## 13. Resumen de fórmulas

```text
ritmo(vuelta)        = base + slope · edad   [+ cliff · max(0, edad − vida_útil)]
estimación           = (k · prior + n · carrera) / (k + n)
restante(alt.)       = Σ vueltas_proyectadas + pérdida_en_boxes [+ tráfico]
ganancia(alt.)       = restante(línea_base) − restante(alt.)
recomendar parada    ⇔  min_{escenarios}(ganancia) > min_gain_s
vida_útil (edad)     = min(usableLife, edad + lifeSpan, edad + desgaste_restante / tasa)
```
