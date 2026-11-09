# Anexo B --- Perfil del fin de semana: datos de práctica y clasificación para la estrategia

**Estado:** propuesta aprobada en principio; **«Fase 3.5»**, entre la Fase 3 (grabación y replay) y la Fase 4
(motor de estrategia mínimo)
**Fecha:** 2026-11-08
**Relacionado:** [`roadmap`](roadmap_ingeniero_carrera_f1_25.md), [`requerimientos`](requerimientos_ingeniero_carrera_f1_25.md)
(RF-04), [`outline`](outline_tecnico_ingeniero_carrera_f1_25.md), [`progress.md`](../progress.md), los planes de las
Fases 2 y 3 y el [`Anexo A`](anexo_a_analisis_por_microsectores.md).

## 1. Objetivo

Que el motor de estrategia de la Fase 4 **arranque la carrera sabiendo lo que ocurrió en la práctica y la
clasificación del mismo fin de semana**, en lugar de partir de cero: cuánto se degrada cada compuesto, cuánto
rinde cada uno, cuánto combustible se consume, cuánto se pierde en boxes y, sobre todo, **qué juegos de
neumáticos quedan disponibles**.

Todo dato heredado de otra sesión se trata como un **prior con confianza explícita**: se usa mientras no haya
datos propios de la carrera y se sustituye gradualmente por ellos.

**Fuera de alcance:** modelos predictivos entrenados, aprender entre fines de semana distintos o entre
circuitos (solo se prevé como ampliación futura), recomendaciones (Fase 4) y cualquier acción sobre el juego.

## 2. Por qué es una fase previa a la Fase 4

-   La estrategia de la Fase 4 compara «parar ahora» con «seguir», y para eso necesita estimar el desgaste y el
    ritmo futuros. En las primeras vueltas de la carrera no hay datos propios suficientes; un prior razonable
    evita que el motor devuelva `INSUFFICIENT_DATA` durante la parte más delicada.
-   La **disponibilidad de neumáticos** es una restricción dura de la estrategia, y solo se conoce con el
    inventario del fin de semana.
-   Se apoya directamente en lo que ya existe: el histórico por vuelta (Fase 2) y el almacén SQLite (Fase 3).
-   La Fase 4 debe funcionar **también sin perfil** (carrera suelta): el perfil es una entrada opcional.

## 3. Qué se puede aprender y de dónde

| Dato | Fuente en el proyecto | Uso en la carrera | Cautela |
| --- | --- | --- | --- |
| Tasa de desgaste por compuesto (`%/vuelta`) | `tyres.wear_rate_pct_per_lap` y registros por vuelta | Prior de degradación | Solo tandas representativas; temperatura de pista |
| Ritmo relativo entre compuestos | Vueltas limpias por compuesto y `lap_delta_time_ms` de `TyreSets` | Diferencia de ritmo entre compuestos | El combustible sesga el ritmo absoluto |
| Consumo de combustible por vuelta | `fuel.consumption_per_lap` | Prior de consumo (confianza baja) | Modo de combustible y plan de la sesión distintos |
| Pérdida en boxes del circuito | `pit.loss_estimate_s`, `pit.last_lane_time_s` | Prior de pérdida por parada | La práctica puede no tener paradas |
| **Inventario de neumáticos** | Paquete `TyreSets` (todos los juegos: compuesto, desgaste, disponibilidad, vida) | Restricción de la estrategia | Hoy el estado solo guarda el juego montado |
| Parrilla | `LapData`/`FinalClassification` | Posición de salida | Ya disponible |
| Temperatura de pista y clima | `env.*` | Contexto del prior | Cambia entre sesiones |

Compuestos: el juego da el compuesto «real» (C0 a C6) y el visual (blando, medio, duro); la correspondencia
varía por fin de semana. El perfil guarda **ambos** y agrupa por el real.

### Limitación importante (combustible y neumáticos)

Dentro de una tanda, el consumo de combustible y la edad del neumático avanzan juntos, de modo que **solo con
tiempos de vuelta no se puede separar** la mejora por quemar combustible de la degradación del neumático. Por eso:

-   El **desgaste medido** (`%` por vuelta, un dato físico del juego) es la señal principal de degradación.
-   El ritmo se reporta en bruto y, **solo si está configurado `fuel_effect_s_per_kg`**, también corregido por
    combustible. No se inventa la corrección.
-   Cada tanda guarda el rango de combustible con el que se corrió, para que el consumidor valore el sesgo.

## 4. Enlace entre sesiones

El parser ya decodifica `season_link_identifier`, `weekend_link_identifier`, `session_link_identifier`,
`weekend_structure` y el tipo de sesión, pero el esquema SQLite v1 de la Fase 3 **no los guarda**. Además
**está por verificar** que el juego los rellene en tu modo de juego (carrera personalizada, carrera de
temporada, etc.).

Estrategia en tres niveles, en este orden:

1.  **Identificadores del juego**, si llegan con valor distinto de cero (`source = game_ids`).
2.  **Agrupación manual**: comando `f1-strategist weekends` para ver las sesiones del mismo circuito y
    unirlas en un fin de semana (`source = manual`).
3.  **Heurística opcional** (mismo circuito y sesiones consecutivas en pocas horas) solo como sugerencia que el
    usuario confirma; nunca se aplica en silencio.

## 5. Modelo de datos (migración v2 sobre el esquema de la Fase 3)

-   `sessions`: nuevas columnas `season_link_id`, `weekend_link_id`, `session_link_id`, `weekend_structure_json`
    y `weekend_id`.
-   `weekends`: `id`, `label`, `track`, `weekend_link_id`, `season_link_id`, `source` (`game_ids`, `manual`),
    `created_at_utc`.
-   `stint_summaries`: por sesión, coche del piloto y tanda: compuesto real y visual, vueltas, edad inicial y
    final, combustible inicial y final, ritmo limpio medio y tendencia (en bruto y corregido si procede),
    desgaste inicial y final y su tasa, temperatura de pista media, clima, número de vueltas limpias,
    `representative` y calidad.
-   `tyre_inventory_snapshots`: por sesión y juego de neumáticos: compuesto real y visual, desgaste,
    disponibilidad, vida restante, vida recomendada, sesión recomendada, diferencia de ritmo y si está montado;
    una instantánea al final de cada sesión y otra al empezar la carrera.
-   `sessions.profile_json` y `sessions.profile_hash`: **el perfil exacto usado** al empezar una carrera. Es
    imprescindible para el determinismo: reproducir una carrera debe dar lo mismo que en vivo, y para eso el
    perfil es una entrada que se registra.

## 6. Diseño

```text
fin de sesión --> resumen de tandas (función pura) --> stint_summaries (SQLite)
                  inventario de neumáticos ---------> tyre_inventory_snapshots
inicio de carrera --> WeekendProfile (construido desde SQLite) --> profile_json/hash en la sesión
                                   |
                                   +--> MetricInputs.profile --> métricas profile.* --> Fase 4
```

-   **Resumen de tandas** (`metrics/stint_summary.py`): función pura sobre los registros por vuelta y las tandas
    de neumáticos; reutiliza las banderas de comparabilidad (sin vueltas de entrada y salida, safety car,
    inválidas, tráfico). Una tanda es **representativa** si tiene al menos 4 vueltas limpias (parámetro).
-   **Perfil** (`weekend_profile.py`): inmutable, con esquema versionado y hash. Por compuesto: tasa de
    desgaste, ritmo relativo respecto a un compuesto de referencia, consumo, pérdida en boxes, número de tandas
    y vueltas usadas, condiciones (temperatura, clima, rango de combustible), procedencia (sesiones y vueltas) y
    calidad. Más el inventario de neumáticos al empezar la carrera.
-   **Mezcla con los datos de la carrera:** `estimación = (k · prior + n · carrera) / (k + n)`, donde `n` es el
    número de vueltas propias válidas y `k` un parámetro (propuesta: 3). Es transparente y se calibra en la
    Fase 9. La Fase 4 decide cómo usarla; aquí solo se ofrece la función y sus pruebas.
-   **Estado**: el estado de carrera conserva hoy solo el juego de neumáticos montado; hay que guardar el
    inventario completo del piloto (13 juegos de seco y 7 de lluvia).
-   **Métricas `profile.*`** (calidad `low` y nota de que son un prior): por ejemplo
    `profile.wear_rate_pct_per_lap` del compuesto montado, `profile.pace_delta_vs_reference_s`,
    `profile.pit_loss_s`, `profile.sets_available` y `profile.confidence`.
-   **Sin perfil**: si no hay sesiones previas, las métricas `profile.*` son `unavailable` y nada falla.

## 7. Requerimiento propuesto

### RF-15 --- Conocimiento del fin de semana (previo a la Fase 4)

-   Enlazar las sesiones de un mismo fin de semana mediante los identificadores del juego o una agrupación
    manual confirmada por el usuario.
-   Resumir por tanda y por compuesto el desgaste, el ritmo, el consumo y la pérdida en boxes de las sesiones
    previas, con procedencia, condiciones, calidad y número de muestras.
-   Conservar el inventario de neumáticos disponible al empezar la carrera.
-   Entregar al motor de estrategia un perfil inmutable y versionado, registrado junto con la sesión, que se
    trate como prior de confianza baja y se sustituya por datos de la carrera a medida que lleguen.
-   Funcionar sin perfil cuando no existan sesiones previas, sin fallar ni inventar datos.

## 8. Tareas

Tamaños: **S** (< ~150 líneas con pruebas) y **M** (~150 a 400). **8 tareas en 3 olas**; todas las tareas de una
ola se pueden hacer en paralelo. Requisito previo: Fase 3 fusionada (almacén `store/` y grabador `recorder.py`).

| ID | Tarea | Tam. | Ola | Depende de | Archivos que posee |
| --- | --- | --- | --- | --- | --- |
| P1 | Migración v2, enlace de sesiones y comando `weekends` | M | 0 | Fase 3 | `store/schema.py`, `store/sqlite_store.py`, `recorder.py` (identificadores), `cli.py` |
| P2 | Estado: inventario completo de neumáticos del piloto | S | 0 | -- | `race_state.py` |
| P3 | Resumen de tandas (función pura) | M | 0 | -- | `metrics/stint_summary.py` |
| P4 | Persistencia de tandas e inventario al cerrar la sesión (en vivo e `ingest`) | M | 1 | P1, P2, P3 | `recorder.py`, `store/` |
| P5 | Perfil del fin de semana, mezcla con prior y serialización | M | 1 | P3 | `weekend_profile.py` |
| P6 | Proveedor del perfil al empezar la carrera, registro en la sesión y comando `weekend show` | M | 2 | P4, P5 | `weekend_provider.py`, `session.py`, `cli.py` |
| P7 | Métricas `profile.*`, `MetricInputs.profile` y exportación | M | 2 | P5 | `metrics/profile.py`, `metrics/engine.py`, `metrics/history.py` |
| P8 | Validación con fin de semana sintético y documentación | M | 2 | P1 a P7 | `tests/`, `docs/`, `progress.md` |

Conflictos conocidos: `cli.py` lo tocan P1 y P6 (un bloque cada una); `recorder.py` lo tocan P1 y P4 (en olas
distintas, sin conflicto); `metrics/history.py` solo recibe un campo nuevo en P7.

### P1 --- Migración v2, enlace de sesiones y comando `weekends` (M)

-   **Hacer:** migración con las columnas nuevas de `sessions` y la tabla `weekends`; el grabador guarda los
    identificadores y la estructura del fin de semana que ya decodifica el parser; comando
    `f1-strategist weekends list|show|link|unlink` para agrupar manualmente sesiones del mismo circuito y
    ver cuáles se enlazaron por identificadores del juego.
-   **Aceptación:** una base v1 se migra sin pérdida; identificadores a cero no crean un fin de semana (se
    tratan como «sin enlace»); `link` rechaza unir sesiones de circuitos distintos.
-   **Pruebas:** migración v1 a v2; sesiones con y sin identificadores; enlace manual y rechazos.

### P2 --- Estado: inventario completo de neumáticos del piloto (S)

-   **Hacer:** conservar en el snapshot el inventario del piloto (juegos de seco y de lluvia) a partir de
    `TyreSets`, con compuesto real y visual, desgaste, disponibilidad, vida y diferencia de ritmo. Los datos
    restringidos de otros jugadores se publican como `None`.
-   **Aceptación:** el inventario se actualiza al llegar el paquete del piloto, se reinicia con la sesión y no
    cambia el panel de consola actual.
-   **Pruebas:** con el fixture de `TyreSets` y escenarios sintéticos.

### P3 --- Resumen de tandas (M)

-   **Hacer:** `summarize_stints(laps, stints, config) -> tuple[StintSummary, ...]` con los campos de la sección 5,
    reutilizando la comparabilidad de la Fase 2; marca `representative` según `min_representative_laps`
    (parámetro nuevo en `MetricsConfig`, por defecto 4).
-   **Aceptación:** pura y determinista; sin vueltas limpias devuelve calidad `insufficient`; el rango de
    combustible de la tanda queda siempre registrado.
-   **Pruebas:** escenario con dos compuestos y una parada; tanda corta; vueltas excluidas por cada bandera;
    con y sin `fuel_effect_s_per_kg`.

### P4 --- Persistencia de tandas e inventario (M)

-   **Hacer:** al cerrar una sesión (fin o cambio), el grabador calcula los resúmenes del piloto y guarda
    `stint_summaries` y una instantánea del inventario; al empezar una carrera guarda la instantánea inicial. El
    mismo camino lo usa `ingest`.
-   **Aceptación:** sin duplicados al reingerir; una sesión interrumpida se cierra con lo que haya y se marca
    `incomplete`.
-   **Pruebas:** sesión sintética completa; interrumpida; reingesta idempotente.

### P5 --- Perfil del fin de semana (M)

-   **Hacer:** `build_weekend_profile(sessions, cfg) -> WeekendProfile` a partir de resúmenes de tandas y de la
    instantánea del inventario (listas en memoria, sin acceso directo a SQLite); `blend_with_prior(prior,
    race_estimate, n_race, k)`; serialización JSON con `schema_version`, y hash estable.
-   **Aceptación:** inmutable y determinista; el hash no depende del orden de entrada; cada valor lleva
    procedencia, número de muestras y calidad; sin datos devuelve un perfil vacío, no una excepción.
-   **Pruebas:** agregación con valores conocidos (mediana ponderada), mezcla con `n = 0` y `n` grande, perfil
    vacío, estabilidad del hash.

### P6 --- Proveedor del perfil y comando `weekend show` (M)

-   **Hacer:** al empezar una carrera, localizar el fin de semana (identificadores o enlace manual), leer los
    resúmenes y el inventario desde SQLite, construir el perfil y entregarlo al motor; registrar
    `profile_json` y `profile_hash` en la sesión. `f1-strategist weekend show [--session ID]` muestra el
    perfil.
-   **Aceptación:** sin fin de semana enlazado no hay perfil y el sistema sigue; al reproducir una carrera
    grabada se usa el perfil **registrado**, no uno recalculado; un fallo de lectura no detiene la captura.
-   **Pruebas:** carrera con y sin perfil; reproducción con perfil registrado; almacén que lanza excepción.

### P7 --- Métricas `profile.*` (M)

-   **Hacer:** añadir `profile: WeekendProfile | None` a `MetricInputs` y al motor; módulo
    `metrics/profile.py` con las métricas de la sección 6; reflejarlas en la exportación JSON.
-   **Aceptación:** calidad máxima `low` y nota de prior; `unavailable` sin perfil; los módulos existentes no
    cambian de comportamiento.
-   **Pruebas:** con y sin perfil; compuesto montado que no existe en el perfil; inventario vacío.

### P8 --- Validación y documentación (M)

-   **Hacer:** extender `tests/scenarios.py` para un fin de semana (práctica, clasificación y carrera con el
    mismo circuito e identificadores) y probar el flujo completo: grabar, cerrar sesiones, construir el perfil,
    registrar el perfil en la carrera y reproducir con la misma huella. Documentar el esquema nuevo y actualizar
    el outline, el roadmap (Fase 3.5) y `progress.md`.
-   **Aceptación:** pasa en Windows y Linux; los fallos indican sesión, compuesto y valor esperado.

## 9. Tareas del usuario

| ID | Qué | Para qué |
| --- | --- | --- |
| UB1 | Confirmar el **modo de juego** (carrera personalizada o de temporada, o fin de semana completo) y grabar **un fin de semana real**: práctica con 2 o 3 compuestos en tandas de unas 8 a 10 vueltas, una clasificación corta y una carrera de al menos 10 vueltas, todo en el mismo circuito y con la captura activa | Validar el flujo con datos reales y comprobar si el juego rellena los identificadores de enlace |
| UB2 | Comprobar en la captura si `weekend_link_identifier` y `session_link_identifier` son distintos de cero | Decidir entre enlace por identificadores o agrupación manual |
| UB3 | Anotar qué programa de práctica o formato de sesión se usó, si el modo los ofrece | Interpretar tandas poco representativas |

## 10. Criterio de salida

1.  Con un fin de semana sintético (práctica, clasificación y carrera), el perfil se construye al empezar la
    carrera con los valores esperados (tolerancia exacta para agregaciones, ±0,01 `%/vuelta` para tasas ajustadas).
2.  El perfil usado queda registrado (`profile_json` y `profile_hash`) y **la reproducción de la carrera da la
    misma huella que en vivo**.
3.  Sin sesiones previas, o con identificadores a cero y sin enlace manual, el sistema funciona y las métricas
    `profile.*` son `unavailable`.
4.  El inventario de neumáticos al empezar la carrera coincide con el último paquete `TyreSets` del piloto.
5.  Con el fin de semana real (UB1), un informe **comparativo** (no de aprobación) muestra la tasa de desgaste
    prevista por la práctica frente a la observada en la carrera, por compuesto. Alimenta la calibración de la
    Fase 9.

## 11. Riesgos y casos límite

| Riesgo o caso | Tratamiento |
| --- | --- |
| El juego no rellena los identificadores de enlace en tu modo | Agrupación manual (`weekends link`) como camino de primera clase |
| Sesión sin vueltas representativas (práctica con programas o pocas vueltas) | `representative` falso, calidad baja y perfil parcial; nunca se inventa |
| Combustible y neumático confundidos en el ritmo | Desgaste medido como señal principal; corrección solo si hay `fuel_effect_s_per_kg` |
| Condiciones distintas entre práctica y carrera (temperatura, lluvia) | Se guardan y se informan; el perfil indica la distancia entre condiciones |
| Compuesto real y visual no coinciden entre fines de semana | Agrupar por el real y guardar ambos |
| Telemetría restringida en otros jugadores | Datos como `None`; el perfil usa solo el coche del piloto |
| Prior que se aferra | Mezcla con `k` pequeño y confianza `low`; se sustituye con datos propios |
| Reproducir una carrera con un perfil distinto del original | Se registra el perfil exacto y la reproducción lo usa |
| Enlace manual equivocado | `link` valida circuito; el perfil muestra la procedencia de cada dato |
| Crecimiento de datos | Resúmenes por tanda, no por vuelta; el inventario son 20 filas por instantánea |

## 12. Decisiones abiertas (propuesta por defecto)

| ID | Decisión | Propuesta |
| --- | --- | --- |
| B1 | Fuente del enlace | Identificadores del juego si existen; agrupación manual si no |
| B2 | Parámetro de mezcla `k` | 3 vueltas, a calibrar en la Fase 9 |
| B3 | Vueltas mínimas de una tanda representativa | 3 vueltas limpias |
| B4 | Alcance temporal del perfil | Solo el mismo fin de semana; el histórico del mismo circuito en otros fines de semana, más adelante |
| B5 | Ritmo de rivales por compuesto | Fuera de la primera versión |
| B6 | Compuesto de referencia para el ritmo relativo | El más blando con tandas representativas; configurable |
