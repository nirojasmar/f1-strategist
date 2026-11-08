# Anexo A --- Análisis de vuelta por microsectores

**Estado:** propuesta aprobada en principio; **posterior al MVP** (no bloquea ninguna fase actual)
**Fecha:** 2026-11-08
**Relacionado:** [`roadmap`](roadmap_ingeniero_carrera_f1_25.md), [`requerimientos`](requerimientos_ingeniero_carrera_f1_25.md),
[`outline`](outline_tecnico_ingeniero_carrera_f1_25.md), [`progress.md`](../progress.md) y el
[`Anexo B`](anexo_b_perfil_del_fin_de_semana.md).

## 1. Objetivo

Decirle al piloto **en qué tramo de la vuelta pierde tiempo** respecto a una vuelta de referencia, y, cuando los
datos lo permitan, **por qué** (frenada más temprana, menor velocidad mínima, salida más lenta). Hoy el
proyecto solo llega a los tres sectores oficiales; este anexo añade una resolución de decenas de tramos.

Ejemplos de uso:

-   Tras una vuelta: «Pierdes 0,12 s en el tramo 14 respecto a tu mejor vuelta; frenas antes.»
-   Pregunta del piloto: «¿Dónde estoy perdiendo tiempo?»
-   Análisis posterior a la sesión: mapa de pérdidas por tramo y «vuelta ideal» (suma de los mejores tramos).

**Fuera de alcance:** coaching en tiempo real durante la curva, nombres oficiales de curvas (el UDP no los
trae), análisis de la telemetría de rivales humanos y cualquier acción sobre el coche.

## 2. Por qué es posterior al MVP

-   El MVP se centra en estrategia de paradas y consultas básicas (roadmap, «Orden recomendado del MVP»).
-   Es más útil en práctica y clasificación que en carrera, y su mensaje llega **al terminar la vuelta**, no
    en plena curva, de modo que no compite con los avisos críticos del Event Gate.
-   Depende de que existan el histórico por vuelta (Fase 2) y el almacén de sesiones (Fase 3).

## 3. Datos disponibles

| Señal | Paquete | Frecuencia observada | Estado en el proyecto |
| --- | --- | --- | --- |
| Distancia en la vuelta (`lap_distance`) | `LapData` | 20 Hz | Decodificado y en el estado |
| Tiempo de vuelta actual (`current_lap_time`) | `LapData` | 20 Hz | Decodificado y en el estado |
| Longitud del circuito y comienzo de sectores 2 y 3 | `Session` | 2 Hz | Decodificado |
| Velocidad, freno, acelerador, volante, marcha | `CarTelemetry` | 20 Hz | **Ignorado por diseño; falta el parser** |
| Posición y velocidad en el mundo, fuerzas G | `Motion` | 20 Hz | **Ignorado por diseño** (no hace falta al inicio) |

Observaciones que simplifican el diseño:

-   **El delta de tiempo por tramo no necesita `CarTelemetry`.** `LapData` da, a 20 Hz, pares de
    (distancia, tiempo de vuelta). Con ellos se construye la curva *tiempo en función de la distancia* de cada
    vuelta y se compara con la de referencia. `CarTelemetry` solo es necesario para explicar **por qué**
    (puntos de frenada, velocidad mínima, aplicación del acelerador).
-   **Resolución:** a 20 Hz y 300 km/h hay una muestra cada ~4 m; es suficiente para tramos de decenas de
    metros. La velocidad también puede derivarse de la curva distancia-tiempo, aunque con más ruido.
-   **Verificado** con la captura real: `CarTelemetry` y `Motion` llegan a 20 Hz y con tamaño igual al de la
    especificación. **Por verificar:** continuidad de `lap_distance` alrededor de la meta, tras *flashbacks* y en
    boxes, y calidad de la curva en vueltas reales.
-   Los fixtures reducidos actuales (Monza) **excluyen** los tipos 0 y 6; para esta función hay que grabar y
    recortar capturas que los conserven.

## 4. Enfoque técnico

```text
LapData (+ CarTelemetry) --> TraceRecorder --> LapTrace (rejilla por distancia)
                                                   |
                    vuelta de referencia ---------+--> DeltaAnalyzer --> delta por microsector
                    Segmenter (tramos por circuito)-/                       |
                                                                  métricas, eventos, consulta del piloto
```

1.  **Traza por vuelta.** Para cada vuelta del piloto se recogen muestras y se remuestrean a una rejilla fija
    de distancia (propuesta: 5 m). Canales: tiempo transcurrido, y opcionalmente velocidad, freno, acelerador,
    volante y marcha. Una vuelta solo es analizable si es comparable (banderas de comparabilidad de la
    Fase 2: sin vuelta de entrada o salida, safety car, vuelta inválida, *flashback*).
2.  **Segmentación.** Se divide la vuelta en microsectores por circuito (`track_id`) y se guarda la
    segmentación para reutilizarla:
    -   *Por defecto*: tramos de igual longitud (propuesta: 40 por vuelta). Simple y estable.
    -   *Mejorada*: tramos detectados en la vuelta de referencia a partir de su perfil de velocidad (zonas de
        frenada y ápices), de modo que cada curva quede dentro de un tramo.
    -   *Opcional*: tabla manual de nombres de curvas por circuito. No se distribuye por defecto.
3.  **Referencia.** Por orden de preferencia: mejor vuelta válida de la sesión actual; mejor vuelta del mismo
    circuito en una sesión anterior del fin de semana (enlaza con el Anexo B); mejor vuelta de un rival si su
    telemetría es pública. Se registra siempre de dónde salió y bajo qué condiciones (combustible, edad del
    neumático, temperatura).
4.  **Delta.** `delta(d) = t_vuelta(d) - t_referencia(d)`. La pérdida de un tramo es la variación del delta
    entre sus extremos. La suma de pérdidas de todos los tramos coincide con la diferencia de tiempo de vuelta
    (con una tolerancia por la última muestra).
5.  **Consistencia.** Una pérdida aislada puede ser ruido (tráfico, combustible, error). El mensaje se emite
    cuando el mismo tramo pierde de forma repetida en varias vueltas (propuesta: al menos 3 de las últimas 5).
6.  **Vuelta ideal.** Suma del mejor tiempo de cada tramo entre las vueltas comparables: indica la ganancia
    potencial.
7.  **Causas (segunda etapa, con `CarTelemetry`).** Reglas transparentes sobre las diferencias de la traza
    frente a la referencia en el tramo: freno más temprano o más largo, velocidad mínima menor, acelerador
    aplicado más tarde. Son heurísticas y se comunican como tales (confianza explícita).

**Normalización.** Comparar vueltas con distinto combustible y desgaste sesga el delta. Se marcan las
condiciones de ambas vueltas, se prioriza el patrón repetido sobre la vuelta suelta y, solo si está
configurado `fuel_effect_s_per_kg`, se ofrece una versión corregida por combustible.

## 5. Salidas

-   **Métricas** (calidad y muestras como el resto): `microsector.loss_s` por tramo de la última vuelta,
    `microsector.top_loss_index`, `microsector.top_loss_s`, `microsector.consistent_loss_index`,
    `microsector.ideal_lap_gain_s`.
-   **Evento informativo** para el Event Gate (Fase 5): `microsector_loss`, con prioridad baja y
    deduplicado, emitido una vez por vuelta como máximo.
-   **Consulta del piloto** (Fase 7): intención `WHERE_LOSING_TIME`, que devuelve el tramo, la pérdida, la
    referencia usada y, si existen, las causas. El LLM solo redacta; las cifras son del código.
-   **Exportación** de trazas y deltas a CSV/JSON para análisis y gráficas.
-   Todo texto visible con claves de mensaje (localizable según RF-13).

## 6. Almacenamiento

Migración SQLite posterior al esquema v1 de la Fase 3:

-   `lap_traces`: `session_id`, `car_index`, `lap_number`, `grid_step_m`, `channels` (JSON con la lista) y un
    `payload` binario comprimido (arrays de `float32`). Estimación: ~1 200 puntos por vuelta con 6 canales, del
    orden de 10 KB comprimidos; una sesión de 60 vueltas ocupa menos de 1 MB.
-   `track_segmentations`: `track`, `version`, `method`, `boundaries_json`.
-   `microsector_results`: resultados por vuelta y tramo, para consultas sin recalcular.

La captura cruda sigue siendo la fuente de verdad: todo esto se regenera con `ingest`.

## 7. Requerimiento propuesto

### RF-14 --- Análisis de vuelta por microsectores (posterior al MVP)

-   Comparar cada vuelta comparable del piloto con una vuelta de referencia por tramos del circuito.
-   Identificar los tramos con mayor pérdida y los que pierden de forma repetida, con calidad y muestras.
-   Cuando exista telemetría de pedales y velocidad, aportar causas probables como hipótesis, no como hechos.
-   Registrar la referencia y las condiciones usadas en cada comparación.
-   Comunicar el resultado tras la vuelta, respetando el silencio por defecto y las prioridades del Event Gate.
-   No inventar nombres de curvas que el juego no proporciona.

## 8. Tareas (esquema; se detallan al acercarse la fase)

Tamaños: **S** (< ~150 líneas con pruebas) y **M** (~150 a 400). Tres olas, todas las tareas de una ola en
paralelo.

| ID | Tarea | Tam. | Ola | Depende de |
| --- | --- | --- | --- | --- |
| M1 | Parser de `CarTelemetry` (22 coches, restringidos a `None`) | M | 0 | -- |
| M2 | Modelo `LapTrace` y remuestreo por distancia (función pura) | M | 0 | -- |
| M3 | Segmentador (tramos iguales y por perfil de velocidad) | M | 0 | -- |
| M4 | Grabador de trazas por vuelta, histórico y migración SQLite | M | 1 | M2, Fase 3 |
| M5 | Selección de referencia, delta por tramo, consistencia y vuelta ideal | M | 1 | M2, M3 |
| M6 | Métricas `microsector.*`, evento informativo, panel de consola y exportación | M | 2 | M4, M5 |
| M7 | Validación (trazas sintéticas con pérdida inyectada; fixture real) y documentación | M | 2 | M4, M5 |

Notas:

-   La **etapa 1** (delta por tramo solo con `LapData`) puede entregarse sin M1; M1 solo habilita las causas
    (etapa 2).
-   **Tareas del usuario:** grabar capturas que conserven los tipos 0 y 6 (sin excluirlos en `trim`); una
    práctica con vueltas consistentes en un mismo circuito; opcionalmente, una tabla de nombres de curvas del
    circuito que más uses.

## 9. Criterio de salida

1.  Con trazas sintéticas con una pérdida conocida inyectada en un tramo, el análisis señala ese tramo como el de
    mayor pérdida con un error de la pérdida ≤ 0,02 s.
2.  La suma de pérdidas por tramo coincide con la diferencia de tiempo de vuelta con tolerancia ≤ 0,02 s.
3.  En una captura real, el análisis es reproducible (misma huella en ejecuciones repetidas, como en la Fase 3).
4.  Las vueltas no comparables se excluyen y el motivo queda registrado.
5.  Las métricas indican `insufficient` o `unavailable` cuando faltan datos, nunca un valor inventado.

## 10. Riesgos y casos límite

| Riesgo o caso | Tratamiento |
| --- | --- |
| Discontinuidades de `lap_distance` (meta, boxes, *flashback*, reinicio a pista) | Descartar la vuelta o el tramo afectado; prueba con escenarios sintéticos y captura real |
| Circuitos con variante inversa o distinta longitud | Segmentación por `track_id` y longitud; no mezclar variantes |
| Vueltas con distinto combustible y neumático | Registrar condiciones; patrón repetido antes que vuelta suelta; corrección opcional |
| Ruido de la rejilla a 20 Hz | Interpolación lineal y suavizado ligero; tolerancias en la sección 9 |
| Demasiados mensajes | Una observación por vuelta como máximo, solo si es consistente |
| Telemetría de rivales restringida | Usar solo referencias propias o públicas; marcar `unavailable` |
| Causas mal atribuidas | Se presentan como hipótesis con confianza; el código no afirma lo que no puede medir |
| Volumen de datos | Rejilla de 5 m, `float32`, compresión; medición en la validación |
| Nombres de curvas no oficiales o con derechos | No se distribuyen por defecto; tabla opcional del usuario |

## 11. Decisiones abiertas (propuesta por defecto)

| ID | Decisión | Propuesta |
| --- | --- | --- |
| A1 | Resolución de la rejilla de distancia | 5 m |
| A2 | Segmentación inicial | 40 tramos de igual longitud; por perfil de velocidad en una etapa posterior |
| A3 | Referencia por defecto | Mejor vuelta válida de la sesión actual; la del fin de semana (Anexo B) si la sesión actual no tiene |
| A4 | Umbral de consistencia | Pérdida en el mismo tramo en 3 de las últimas 5 vueltas comparables |
| A5 | Telemetría de rivales | Fuera de la primera versión |
| A6 | Nombres de curvas | Fuera de la primera versión; tabla opcional por circuito |
