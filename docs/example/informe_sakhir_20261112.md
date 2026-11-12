# Informe de captura real: carrera de alta degradación en Sakhir

## Alcance

Informe de seguimiento para la validación del motor de estrategia de Fase 4. No
es una prueba de aprobación ni demuestra por sí solo el cierre de S10. Se analizó
la captura local `captures\session-20261112-162235.f1cap` sin modificarla. El
usuario reportó las observaciones del HUD incluidas en este informe; no se
dispone de una grabación visual de la pantalla. La investigación posterior
utilizó las evaluaciones de estrategia guardadas y una reproducción del recorte
de carrera para identificar sus causas y limitaciones.

- Archivo original: 674.036.037 bytes, SHA-256
  `be5192c4b2a94d1d0f38f05e4f7e222651d5fdf3f087d06b0d863f53c9dbf372`.
- `inspect`: 645.771 paquetes, 4.635,0 s entre el primero y el último paquete,
  cero cabeceras inválidas y sin truncamiento.
- El fin de semana contiene tres sesiones vinculadas: práctica `practice_1`,
  clasificación `short_qualifying` y carrera `race`. Todas indican Sakhir; la
  carrera tiene 20 vueltas y una longitud de 5.408 m.
- Se generaron dos recortes independientes y anonimizados para el
  [manifiesto de capturas reales](../../tests/fixtures/real/manifest.toml):
  [práctica](../../tests/fixtures/real/sakhir-practice-12-laps.f1cap.gz) y
  [carrera](../../tests/fixtures/real/sakhir-race-20-laps-high-wear.f1cap.gz).
  Se conservaron paquetes de sesión, vueltas, eventos, participantes, estado,
  daños, historial de tandas, juegos y posiciones; estado e historial se
  diezmaron a uno de cada dos paquetes. El recorte de carrera conserva los
  eventos `SCAR`.
- Las expectativas de ambos escenarios se calcularon de nuevo desde los
  recortes: 22 coches, juego 1.26; práctica, mejor vuelta 90,291 s; carrera,
  mejor vuelta 91,699 s y 22 paradas contabilizadas en la clasificación final.

## Degradación y parada del piloto

La telemetría de carrera confirma un stint inicial con C3 blando y una parada en
la vuelta 9 por C1 duro. El desgaste medio de las cuatro ruedas al cierre de la
vuelta 8 fue **46,25 %**. La pérdida de posiciones inmediatamente tras la parada
es visible en los datos: el piloto pasó de P1 a P13 y volvió a P1 en la vuelta
12.

| Vuelta | Compuesto visual | Edad reportada | Desgaste medio al cierre | Posición |
| ---: | --- | ---: | ---: | ---: |
| 1 | Blando (C3) | 0 | 6,25 % | 1 |
| 4 | Blando (C3) | 3 | 23,50 % | 1 |
| 8 | Blando (C3) | 7 | **46,25 %** | 1 |
| 9 | Duro (C1) | 0 | 2,00 % | 13 |
| 12 | Duro (C1) | 3 | 11,50 % | 1 |
| 20 | Duro (C1) | 12 | 31,75 % | 1 |

El usuario reportó que el juego mostraba estas dos estrategias de carrera:

1. Blando hasta la vuelta 10 y medio hasta el final.
2. Blando hasta la vuelta 9 y duro hasta el final.

La telemetría respalda que se ejecutó una parada en la vuelta 9 con duro; no
demuestra cuál era la estrategia óptima ni que parar compensara frente a
mantener. Tampoco sustituye la carrera pendiente en la que mantener sea
claramente mejor.

## Safety car

La carrera tuvo un safety car completo observable tanto en el estado de sesión
como en cuatro eventos `SCAR`: despliegue durante la vuelta 17, retorno y
reanudación en la vuelta 18. El estado pasó a `full_safety_car`; no fue solo una
observación visual del piloto.

El safety car está fuera del alcance del MVP de esta fase. Esta captura deja
constancia de que el modelado posterior del motor debe tratarse como prioridad:
el estado cambia el coste de parar y las ventanas/posiciones de reincorporación.

## Observaciones reportadas del HUD

Lo siguiente es observación del usuario durante la carrera, no una conclusión
derivada de la captura de telemetría:

- Durante la mayor parte de la carrera no apareció recomendación por «datos
  insuficientes».
- «Mantener» apareció durante unas cuatro vueltas desde aproximadamente la
  vuelta 10; después volvió a aparecer que faltaban datos.
- El dato de reincorporación desapareció durante aproximadamente dos vueltas,
  poco después de que todos pararan.
- Compuesto, juego, vuelta de parada, ganancia contra la línea base e intervalo
  de ganancia permanecieron como «—».

## Hallazgos sobre el HUD

### Evidencia y límites

En `captures\sessions.db`, la sesión de carrera local **12** tiene el mismo
SHA-256 de captura indicado arriba, el perfil de fin de semana registrado y
**1.932 evaluaciones**: 1.699 `INSUFFICIENT_DATA`, 223 `NO_CHANGE` y 10
`PIT_NOW`. Son evaluaciones registradas durante la sesión, no una grabación de
la pantalla; no se puede afirmar qué actualización concreta alcanzó a ver el
piloto. No hay errores de aplicación registrados para esta sesión.

Se reprodujo además el recorte de carrera con la configuración y el perfil
guardados de la sesión 12, tomando muestras aproximadamente a mitad de cada
vuelta. La reproducción terminó sin rechazos ni truncamiento. Al estar
anonimizado y diezmado, el recorte sirve para comprobar las entradas y la lógica,
pero no para reconstruir exactamente cada actualización en vivo.

### 1. Cambios de recomendación

| Momento registrado | Acción y motivo | Causa |
| --- | --- | --- |
| Vueltas 1–4 | `INSUFFICIENT_DATA`: `pace_quality_insufficient` y `pit_loss_unavailable` | Faltaban tanto un ajuste de ritmo de carrera como una fuente de pérdida en boxes. El perfil guardado no tenía muestras de pérdida en boxes ni ritmo utilizable de C3. |
| Vueltas 5–8 | `INSUFFICIENT_DATA`: `pit_loss_unavailable` | El ritmo dejó de bloquear, pero seguía sin pérdida en boxes: no había anulación configurada ni una muestra utilizable del campo, perfil o carril. |
| Inicio de vuelta 9 | Diez evaluaciones `PIT_NOW`, con `mandatory_stop` y riesgo `tyre_life_exceeded` | Se llegó a evaluar una parada obligatoria; la recomendación guardada era C2, juego 10. Por tanto, no es correcto afirmar que todos los campos permanecieron vacíos en *todas* las evaluaciones. |
| Resto de vuelta 9–12 | `INSUFFICIENT_DATA`: `stay_out_unavailable` | Tras montar C1, no había todavía ajuste suficiente de su tanda. El perfil de C1 no podía anclarse al ritmo de referencia C3, pues el perfil no tenía el delta de C3. En las muestras reproducidas no existía modelo C1, aunque sí se evaluaban algunas alternativas C3. |
| Vuelta 13 hasta parte de la 15 | `NO_CHANGE`: `decision_default_no_change` | Al acumular tres vueltas limpias de C1 se pudo ajustar su ritmo y evaluar `stay_out`; ninguna parada elegible y estable superó el umbral conservador de ganancia. |
| Parte de vuelta 15–17 y vueltas 19–20 | `INSUFFICIENT_DATA`: `stay_out_unavailable` | La evaluación descartó continuar por una comparación incorrecta de vida del neumático, descrita abajo. Al final también aparecen vueltas restantes no disponibles y datos obsoletos. |
| Neutralización en vueltas 17–18 | `INSUFFICIENT_DATA`: `safety_car_conditions`, `unmodeled_condition` | El motor suspende deliberadamente la recomendación durante un safety car, condición que todavía no modela. |

La aparición de «Mantener» coincide así con las vueltas **13–15** del registro,
no con una transición confirmada en la vuelta 10. La hora visual aproximada
reportada no permite resolver esa diferencia.

La recomendación breve de vuelta 9 merece una comprobación adicional: en el
registro, la vuelta de entrada del piloto cerró con solo **1,933 s** de
tiempo de carril y la de salida con **23,933 s**. La reproducción muestra que
el estimador usa esa primera cifra como fuente `lane` mientras aún no tiene una
muestra completa del campo; después utiliza muestras del campo de alrededor de
22–24 s. El código acepta el tiempo de una vuelta de entrada cerrada sin
esperar a la salida. Es una pérdida potencialmente parcial, incompatible con
tratarla como coste completo de una parada; la evaluación guardada no conserva
la fuente ni el valor de pérdida, por lo que su uso exacto en cada una de las
diez recomendaciones en vivo no queda demostrado.

La causa concreta del retorno prematuro a `stay_out_unavailable` es una
**mezcla de vida restante y edad total**. La especificación local
([estructuras de telemetría](../../plans/F1%2025%20Telemetry%20Output%20Structures.txt))
define `m_lifeSpan` como vueltas *restantes* del juego, mientras
`m_usableLife` es el máximo recomendado para el compuesto. Sin embargo,
[`_usable_life`](../../src/f1_strategist/strategy/pace_model.py) toma el mínimo
de ambos como si fuera una edad total, y
[`_stay_out`](../../src/f1_strategist/strategy/evaluator.py) lo compara con
`edad actual + vueltas restantes de carrera`.

En la muestra reproducida de la vuelta 15, el C1 tenía edad **6**, el juego
indicaba **11 vueltas restantes** y máximo recomendado **16**; quedaban unas
**5,5 vueltas** de carrera. El código compara `6 + 5,5 > 11` y descarta
continuar, pese a que 5,5 es menor que las 11 vueltas restantes indicadas. En
la vuelta 14, `5 + 6,5 <= 12`, por lo que la misma lógica todavía admitía
continuar. El registro en vivo confirma el cambio a `stay_out_unavailable`
durante la vuelta 15. Esto identifica un error de unidades temporales, no prueba
que el neumático fuera necesariamente óptimo o seguro hasta el final.

### 2. Campos vacíos y reincorporación

[`render_strategy_rows`](../../src/f1_strategist/console_messages.py) muestra
compuesto, juego, vuelta y ganancias de la **recomendación**, no de cualquier
alternativa calculada. Para `NO_CHANGE` e `INSUFFICIENT_DATA`, la decisión no
rellena esos campos, de modo que aparecen «—» aunque existan alternativas. El
guion mezcla por tanto al menos «no aplica a la acción», «no disponible» y
«calculado en otra alternativa»; por sí solo no demuestra falta de cálculos.

La reincorporación también se toma únicamente de la alternativa cuya clave
coincide con la recomendación. En `NO_CHANGE` esa clave es `stay_out`, que no
tiene reincorporación: desaparece del HUD aunque las alternativas de parada sí
la calculen. Por ejemplo, en una evaluación de la vuelta 13 `stay_out` no tenía
posición y una alternativa de parada indicaba **P11**. Esto explica la ausencia
durante el intervalo registrado de `NO_CHANGE`, sin necesidad de suponer que
desaparecieron los huecos de todos los rivales.

Cuando la acción es `INSUFFICIENT_DATA`, la clave puede ser la línea base de
una parada sin que exista una recomendación de parar; el HUD puede entonces
mostrar la reincorporación de esa alternativa. Por ejemplo, en la vuelta 10
se guardó P19 para `pit_now:c3:set5`, pese a `INSUFFICIENT_DATA`. Mostrar ese
valor sin identificarlo como alternativa o referencia puede inducir a error.
Además, [`estimate_rejoin`](../../src/f1_strategist/strategy/pit_model.py)
necesita una pérdida en boxes y huecos suficientes; si no existen, la posición
puede faltar incluso en alternativas de parada. La estimación supone que los
rivales no paran.

## Seguimiento recomendado

1. Corregir la comparación entre vida restante y edad total del juego montado;
   añadir regresiones con los valores de las vueltas 14–15 y comprobar también
   los límites de vida recomendada. Revisar la aceptación de tiempos de carril
   parciales y cómo disponer de pérdida en boxes y de un modelo C1 al inicio de
   su tanda, sin inventar datos.
2. Distinguir en el HUD campo desconocido, no aplicable y alternativa calculada
   sin recomendación; identificar explícitamente la referencia de cualquier
   reincorporación mostrada y cubrir `NO_CHANGE` e `INSUFFICIENT_DATA` con
   alternativas calculadas.
3. Investigar un prior externo, obtenido de carreras reales, para estimar la
   posición de reincorporación incluso antes de la primera parada rival.
   Presentarlo como estimación con incertidumbre y refinarlo cuando las
   posiciones y huecos en vivo permitan calcularla con mayor precisión.
4. Después del MVP, priorizar el modelo de safety car en el motor y añadir
   escenarios de regresión que cubran su despliegue, permanencia y retirada.
5. Completar UE2 con una carrera real donde mantener sea claramente mejor; esta
   captura solo cubre el caso de alta degradación y una estrategia de una parada.
