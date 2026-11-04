# Progreso del proyecto --- F1 Strategist

**Última actualización:** 2026-11-03
**Propósito de este archivo:** llevar trazabilidad del avance importante y dejar el contexto
necesario para que cualquier persona o agente que llegue al proyecto entienda dónde estamos, qué
está decidido, qué está verificado y qué falta.

**Cómo mantenerlo:**

-   Actualizarlo al cerrar cada hito, decisión o hallazgo relevante (no por cada commit).
-   Añadir entradas nuevas a la bitácora (sección 6) con fecha; no reescribir las anteriores.
-   Distinguir siempre entre lo **verificado** (con evidencia) y lo **supuesto** o provisional.
-   Los documentos de diseño (`docs/`) dicen *qué se quiere construir*; este archivo dice *qué se ha
    hecho y qué se sabe*.

## 1. Resumen

Aplicación local que recibe la telemetría UDP de **F1 25 (2026 Season Pack)**, mantiene un estado de
carrera, evalúa estrategias y actúa como ingeniero de carrera virtual (LLM local para explicar y
conversar; cálculos y decisiones en código determinista). Se ejecuta en el mismo PC del juego o en
otro PC de la red local. Primera interfaz: consola; GUI web local y localización (inglés y español)
más adelante.

Documentos de diseño:

-   [`docs/requerimientos_ingeniero_carrera_f1_25.md`](docs/requerimientos_ingeniero_carrera_f1_25.md)
-   [`docs/outline_tecnico_ingeniero_carrera_f1_25.md`](docs/outline_tecnico_ingeniero_carrera_f1_25.md)
-   [`docs/roadmap_ingeniero_carrera_f1_25.md`](docs/roadmap_ingeniero_carrera_f1_25.md)

## 2. Reglas de colaboración (para agentes)

-   **No editar código si el usuario no lo pide de forma directa.** Proponer el cambio y esperar
    confirmación. (Indicado por el usuario el 2026-11-01.)
-   Comunicación y documentación en **español**.
-   Ante la duda sobre el alcance de un cambio, preguntar antes de actuar.
-   Distinguir en las respuestas lo medido, lo estimado y lo supuesto; no inventar datos del juego.

## 3. Decisiones vigentes

| Tema | Decisión | Origen / estado |
| --- | --- | --- |
| Juego objetivo | F1 25 (2026 Season Pack) | Usuario (2026-10-31) |
| Versión observada | Juego 1.26, formato UDP 2025, año 25 | Verificado con captura real (2026-11-01) |
| Sistemas operativos de la app | Windows y Linux (Ubuntu/Mint) | Usuario (2026-10-31) |
| Lenguaje y entorno | Python 3.12+, `uv`, `ruff`, `pyright`, `pytest` | Acordado (2026-10-31) |
| Modelos y configuración | `pydantic` v2 y TOML | Acordado |
| LLM | Cliente HTTP a API compatible con OpenAI, adaptador intercambiable | Acordado; modelo concreto pendiente |
| Almacenamiento | Captura binaria cruda (`.f1cap`) + SQLite para derivados | Acordado; esquema SQLite pendiente |
| Interfaz | Consola primero; GUI web local (FastAPI + WebSocket) después | Acordado |
| Topologías | Mismo PC o PC de la red local; solo cambia la configuración | Requisito del usuario |
| Seguridad de red | Escucha solo en loopback por defecto; apertura a la LAN explícita | Requisito |
| Receptor UDP | Hilo dedicado con socket bloqueante y cola acotada | Decisión cerrada; `overall_frame_identifier`, cadencia observada y retención limitada |
| Formato `.f1cap` v1 | Definido en `src/f1_strategist/capture.py` | Implementado en Sprint 1 |
| Parser | Registro de decodificadores `struct` precompilados; modelos inmutables | Nueve tipos; formato 2025, año 25, juego 1.x y paquete v1; otras versiones menores se avisan y requieren tamaño exacto |
| Estado de carrera | Un consumidor escritor publica snapshots inmutables | Sesión (entorno, pronóstico, safety car), coches (tiempos, intervalos, boxes, combustible/ERS, neumáticos), frescura por grupo y eventos; las métricas derivadas quedan en Fase 2 |

## 4. Estado por fase del roadmap

| Fase | Estado | Notas |
| --- | --- | --- |
| 0. Definición y entorno | Parcial | Repo, CI definido, config, receptor y guía de firewall listos. Falta validar la recepción desde otro PC de la red local y en Linux real. |
| 1. Receptor, parser y estado | Implementada; CI verde | Snapshots inmutables de sesión y coches, daños, frescura por grupo, eventos y resumen de consola. |
| 2. Métricas e histórico | En curso | A1--A4, B1--B3, histórico C1--C4/C2b y métricas D1--D9 implementados; faltan integración (E) y cierre (F). |
| 3. Grabación y replay | Parcial | Captura y reemisión UDP listas (se adelantaron en Sprint 1). Falta el replay interno hacia el pipeline y el registro en SQLite. |
| 4--10 | No iniciadas | |

## 5. Qué está verificado y qué no

**Verificado:**

-   La suite `pytest` (221 pruebas), Ruff (lint y formato) y Pyright pasan en Windows (2026-11-03).
-   CI de GitHub Actions exitoso para `phase-1-udp-parser`, commit `096eceb`, en Ubuntu y Windows; también
    pasaron Ruff (lint y formato) y Pyright (2026-11-02).
-   El consumidor único aplica los paquetes parseados al estado y publica snapshots inmutables; la frescura
    usa el reloj monotónico de recepción. La consola presenta vuelta, posición, combustible, compuesto y eventos.
-   Flujo de extremo a extremo en Linux con procesos reales: `replay` -> `run` -> captura -> `inspect`.
-   **Recepción de telemetría real en Windows 11** con el juego: captura de 68 541 paquetes en
    ~13 minutos, sin paquetes inválidos.
-   La cabecera de 29 bytes y los IDs de tipo de paquete (0 a 15) coinciden con la especificación oficial
    de EA (formato 2025) y funcionan con la versión 1.26 del Season Pack.
-   El parser valida formato 2025, año 25, versión mayor 1 y paquete v1 con tamaño exacto. Acepta
    versiones menores distintas de 1.26 con aviso; rechaza protocolos incompatibles y cuenta los tipos
    excluidos como ignorados por diseño.
-   Los nueve tipos implementados (`Session`, `LapData`, `Participants`, `CarStatus`,
    `CarDamage`, `TyreSets`, `SessionHistory`, `Event`, `FinalClassification`) se decodificaron contra
    datagramas de la captura real; sus tamaños medidos son 753, 1285, 1284, 1239, 1041, 231, 1460,
    45 y 1042 bytes, respectivamente.
-   Los valores desconocidos en campos enumerados quedan representados como `EnumValue(name="unknown")`
    y conservan el valor original. Errores y diagnósticos usan claves y parámetros; la consola los
    renderiza en español.
-   Los fixtures compactos de `tests/fixtures/` cubren los nueve tipos; se extrajeron de la captura real y
    anonimizan nombres de participantes e identificadores de red.
-   Hypothesis prueba datagramas arbitrarios y cuerpos aleatorios con cabeceras válidas; solo se permiten
    rechazos tipados del parser.
-   Los tamaños de los 14 tipos de paquete presentes en la captura coinciden exactamente con los de la
    especificación.
-   El orden de campos de `LapData` de la especificación se verifica con el fixture de captura real
    (sección 7).
-   `LapCarData.grid_position_zero_based` conserva la base 0 del paquete; el estado de carrera lo expone
    como `grid_position` base 1, igual que `FinalClassification.grid_position`.
-   La captura es una carrera de 5 vueltas con 22 coches.
-   Ejecución en Linux en red local captura adecuadamente los paquetes del primer PC.
-   `CaptureReader` acepta entradas `.f1cap.gz`; `trim` conserva el tiempo de recepción y permite filtrar
    por tipo, decimar paquetes, seleccionar un rango de vueltas y anonimizar identificadores.
-   El constructor de escenarios sintéticos genera los tipos de paquete de carrera requeridos y puede
    aplicar safety car, cambio de clima, vueltas inválidas y paradas programadas.
-   El runner offline procesa `.f1cap` sin red, hilos ni esperas, entrega paquetes parseados a
    consumidores con tiempo monotónico simulado y cuenta rechazos, tipos ignorados y truncamiento.
-   `MetricsConfig` ofrece valores predeterminados validados para métricas y parámetros opcionales
    desactivados por defecto.
-   Los tipos base de métricas (`Quality`, `Window`, `Metric`) y sus helpers están implementados
    con invariantes de calidad y pruebas unitarias.
-   B1: `CarSnapshot` publica tiempos de vuelta y sectores, intervalos, distancia, invalidación de vuelta,
    penalizaciones, estado de resultado y estado/tiempos de boxes. Los ceros de tiempo y los deltas del
    líder se normalizan a `None`; los coches `invalid`/`inactive` no publican valores de vuelta.
-   B2: `CarSnapshot` publica capacidad y combustible restante, ERS (almacén, modo, cosechado y desplegado),
    bandera FIA, permiso de DRS y el neumático montado (`FittedTyreSnapshot`). `TyreSets` ya puebla el
    coche indicado por `car_index`; un `fitted_index` ausente deja `fitted_tyre=None`. Con capacidad de
    combustible 0, combustible y ERS quedan en `None` en lugar de publicarse como 0.
-   B3: `RaceSessionSnapshot` publica clima, temperaturas, estado de safety car, tiempo restante, longitud
    del circuito, límite de velocidad en boxes, ventanas de parada, posición de reincorporación,
    `game_paused` y el pronóstico por muestras (`WeatherForecastSnapshot`). `safety_car_changed` y
    `weather_changed` se emiten solo ante transiciones entre valores conocidos; un cambio de sesión limpia
    todo el entorno y el pronóstico.
-   Ola 2 (C1--C4): implementados el histórico acotado e inmutable por vuelta, el cierre y muestreo en las
    fronteras, la ingesta idempotente de `SessionHistory` (incluidos tiempos oficiales, validez y stints) y
    la clasificación pura de comparabilidad.
-   C2b, según la review de Ola 2: corregidas la edad de neumático al cerrar la vuelta de entrada y su
    reinicio al cambiar neumáticos con la vuelta abierta; marcadas las vueltas parciales e incompletas;
    excluidos del delta de daño los desgastes graduales; detectados flashbacks intra-vuelta y vueltas de
    reinicio tras bandera roja. `LapTracker.observe` evita recorrer coches si no cambian las marcas de
    frescura relevantes, conservando el manejo de cambio y fin de sesión.
-   Validación local Windows tras C2b: `uv run pytest -q` (308 pruebas), `uv run ruff check .`,
    `uv run ruff format --check .`, `uv run pyright` y `git diff --check` pasan.
-   Implementados D1--D9 como módulos `MetricModule` y registrados en `metrics/registry.py`: progreso,
    combustible, neumáticos, ritmo, intervalos y comparación con rivales, boxes, ERS, daños y entorno.
    Las muestras derivadas respetan calidad, frescura y datos faltantes; las ventanas de combustible,
    neumáticos y ritmo excluyen las vueltas parciales, incompletas o retiradas cuando corresponde.
-   `LapRecord` conserva la temperatura de pista media observada durante la vuelta para calcular la
    tendencia por vuelta de D9. La capacidad de ERS y el límite de desgaste siguen sin valores por defecto;
    las métricas que dependen de ellos quedan `unavailable` hasta configurarlos.
-   Validación local Windows tras D1--D9: `uv run pytest -q` (351 pruebas), Ruff (lint y formato),
    Pyright y `git diff --check` pasan.
-   Aplicadas las correcciones pertinentes de `review_fase_2_ola_3.md` (R1--R10): aislamiento y
    continuidad de las tendencias por sesión/coche/rival; selección del stint actual sin tomar stints
    futuros ni vueltas del juego anterior; exclusión de paradas con vueltas duplicadas o no comparables;
    ERS limitado a vueltas completas; frescura de sesión en márgenes de combustible; vuelta de formación
    excluida del progreso; y temperatura muestreada una vez por actualización fresca de sesión con memoria
    constante por vuelta.
-   Añadidas regresiones para los casos R1--R10, incluidos paquetes repetidos, cambio de rival, stint
    futuro o reinicio de neumático, datos obsoletos, vueltas inválidas y temperatura repetida/obsoleta.
-   Validación local Windows tras la review: `uv run --group dev pytest -q` (370 pruebas),
    `uv run --group dev ruff check .`, `uv run --group dev ruff format --check .` (70 archivos),
    `uv run --group dev pyright` (0 errores/avisos) y `git diff --check` pasan.

**No verificado todavía:**

-   C2b usa escenarios sintéticos; falta contrastar con una captura real en F3 el orden de cambio de
    neumáticos, el tiempo de la vuelta final y la ausencia de `damage_change` en vueltas sin incidentes.
-   Las métricas D1--D9 se validaron con escenarios sintéticos; falta calibrarlas con una captura real.
-   No se validó todavía la integración E1--E4 ni se volvió a ejecutar CI de GitHub Actions tras las
    correcciones de esta review.

## 6. Bitácora

### 2026-10-31

-   Revisión de los tres documentos iniciales y análisis de gaps.
-   Añadidos los requisitos: ejecución en el mismo PC o en la red local (RF-11), consola primero y GUI
    después (RF-12), localización inglés/español (RF-13); Fase 10 del roadmap y nuevos riesgos.
-   Stack acordado y registrado en el outline (sección 8) y en las decisiones de los requerimientos.
-   Sprint 1 entregado en dos parches: esqueleto Python, receptor UDP, captura `.f1cap` v1, reemisión
    UDP (`replay`), `inspect`, consola con Rich, configuración TOML, pruebas y CI; y actualización de docs
    (juego objetivo, sistemas operativos, nota del parser).
-   Búsqueda de librerías de referencia: `F1Game.UDP` (.NET) y un parser en TypeScript
    (`@deltazeroproduction/f1-udp-parser`) soportan F1 25. No se encontró un parser en Python verificado.
-   La página web de la especificación de EA no se pudo leer con las herramientas de desarrollo. La
    especificación sí estaba disponible desde el inicio como archivos del proyecto (ver 2026-11-01).

### 2026-11-01

-   Primera ejecución en Windows 11: la captura salió con 0 paquetes. **Causa: el juego tenía Broadcast
    Mode activado**; con la app escuchando solo en `127.0.0.1` no llegaban datos. Solución: escuchar en
    `0.0.0.0` (`--listen-address 0.0.0.0`).
-   Con esa configuración se grabó la primera captura real (ver sección 7).
-   El usuario indicó que no se edite código sin petición directa.
-   El usuario señaló la documentación UDP de EA (formato 2025) y un comentario de foro sobre el orden de
    `m_gridPosition` y `m_driverStatus` en `LapData`. Se contrastaron la cabecera, los IDs de paquete y el
    tamaño de `LapData` (57 bytes por coche, 1 285 por paquete) con la especificación: coinciden.
-   **Corrección:** la cadencia anotada antes (~11 Hz, ~85 KB/s) se calculó sobre la duración total de la
    captura y era errónea. Con las frecuencias fijas de la especificación, el envío es de 20 Hz (sección 7).
-   Verificación sobre la captura real: los 14 tipos de paquete miden exactamente lo que declara la
    especificación. En `LapData`, el offset 43 (`m_gridPosition` según la especificación) trae 22 valores
    distintos de 0 a 21, constantes en toda la captura, y el offset 44 (`m_driverStatus`) vale 4 en todos los
    coches. El comentario de foro sobre el orden intercambiado no se confirma en esta versión.
-   Cierre del receptor: se mantiene el hilo dedicado; a la carga observada no le aporta valor migrar a
    `asyncio`. Se miden huecos de frame estimados, duplicados y desorden por tipo, aislados por
    `session_uid`, y se muestran en consola. Los huecos no confirman pérdida porque la cadencia cambia
    entre tipos de paquete; UDP no reenvía.
-   Cruce de `LapData` y `FinalClassification` sobre la captura: `LapData.m_gridPosition` es la parrilla menos 1
    para los 22 coches (base 0), mientras que `FinalClassification.m_gridPosition` y `m_carPosition` al inicio
    coinciden en base 1. La sesión es una carrera de 5 vueltas con 22 coches.
-   Creado este archivo.

### 2026-11-02

-   Atendidos los comentarios de `review.md`: corregidos los layouts de stints en `SessionHistory` y
    `FinalClassification`, incluidos contadores y valores opcionales; se añadieron fixtures anonimizados,
    pruebas con valores no nulos, validaciones de rango y pruebas basadas en Hypothesis.
-   Se completaron además los cambios de versión, clasificación de paquetes ignorados, secuencias UDP,
    límites de memoria, aislamiento del consumidor, modelos de mensajes/localización y documentación.
-   Validación local en Windows: `pytest` (70 pruebas), Ruff (lint y formato) y Pyright pasan.
-   Replay de `captures/session-20261101-115103.f1cap`: se decodificaron 41 214 datagramas de los nueve
    tipos implementados, sin errores de parser.
-   Replay de `captures/session-20261101-233816.f1cap`: se verificó la captura y el estado (ver sección
    7). Los paquetes `CarDamage` quedan en los snapshots por coche, sin derivar métricas.
-   Replay de estado de la misma captura: los nueve tipos se aplicaron sin errores; se conservaron los 22
    nombres de `Participants` y las muestras del piloto. Los eventos `Event` con `session_uid=0` no
    alteran la sesión identificada por `Session`.
-   Se realizó una prueba ejecutando el receptor en el segundo PC con Linux, mientras el   primer PC (Windows) corría el juego, prueba hecha sobre el código de `main`.
-   Implementado el estado de carrera de un solo escritor: sesión, 22 coches con nombres de `Participants`,
    coche del piloto, muestras desconocidas como `None`, frescura por grupo y hasta 50 eventos recientes.
-   Se detectan cambio de sesión, inicio/final, vuelta y entrada/completado de parada en boxes. El panel
    muestra vuelta, posición, combustible y compuesto; no calcula métricas derivadas (Fase 2).
-   La prueba con la captura real confirmó que algunos eventos `buttons` traen `session_uid=0` mientras
    los demás paquetes usan el UID de carrera; solo `Session` y eventos de ciclo de vida pueden cambiar
    la identidad que mantiene el estado.
-   Implementados F1 y pruebas de recorte, decimación, rango de vueltas, anonimización y lectura gzip.
    Suite completa: 90 pruebas; Ruff y Pyright pasan; `trim --help` muestra las opciones esperadas.
-   Implementados A1 (tipos base de métricas) y pruebas de umbrales, inmutabilidad, helpers y ventanas.
    En ese punto, la suite tenía 110 pruebas; Ruff y Pyright pasaban.
-   Implementada A2: configuración Pydantic para métricas, sección `[metrics]` de ejemplo y pruebas de
    valores por defecto, TOML válido, parámetros opcionales y rechazo de valores inválidos. En ese punto,
    la suite tenía 99 pruebas; Ruff y Pyright pasaban; el TOML de ejemplo cargaba correctamente.
-   Implementados A3 (runner offline) y sus pruebas deterministas con fixtures, rechazos, paquetes
    ignorados y captura truncada. En ese punto, la suite tenía 90 pruebas; Ruff y Pyright pasaban.
-   Implementados A4 (`tests/scenarios.py`) y pruebas de paquetes, determinismo, cabeceras, estado final
    e invariantes de entrada. Suite completa: 93 pruebas; Ruff y Pyright pasan.

### 2026-11-03

-   Implementada B1 (estado de tiempos, intervalos y boxes): `CarSnapshot` añade `last_lap_time_ms`,
    `current_lap_time_ms`, `sector1_time_ms`, `sector2_time_ms`, `delta_to_car_in_front_ms`,
    `delta_to_leader_ms`, `lap_distance_m`, `current_lap_invalid`, `penalties_s`, `result_status`,
    `pit_lane_timer_active`, `pit_lane_time_ms` y `pit_stop_timer_ms`. Los tiempos y timers a 0 se
    normalizan a `None`, el delta del líder no se publica para la posición 1 y los coches `invalid` o
    `inactive` no publican datos de vuelta.
-   Implementada B2 (combustible, ERS y neumáticos montados): `CarSnapshot` añade `fuel_capacity_kg`,
    `fuel_remaining_laps`, `ers_store_j`, `ers_deploy_mode`, `ers_harvested_mguk_j`,
    `ers_harvested_mguh_j`, `ers_deployed_this_lap_j`, `fia_flag`, `drs_allowed` y `fitted_tyre`.
    `FittedTyreSnapshot` expone compuestos, `wear_pct`, `lifespan_laps` y `usable_life_laps`.
    `TyreSetsPacket` se aplica al coche de `car_index` usando `fitted_index`; `255` o un índice inválido
    dejan `fitted_tyre=None`. Con `fuel_capacity == 0`, combustible y ERS quedan en `None`.
-   Implementada B3 (entorno de sesión): `RaceSessionSnapshot` añade `weather`, `track_temperature_c`,
    `air_temperature_c`, `safety_car_status`, `session_time_left_s`, `track_length_m`,
    `pit_speed_limit_kph`, `pit_window_ideal_lap`, `pit_window_latest_lap`, `pit_rejoin_position`,
    `forecast` y `game_paused`; `WeatherForecastSnapshot` representa cada muestra del pronóstico. Las
    ventanas de boxes y la posición de reincorporación con valor 0 se publican como `None`.
-   Añadidos los eventos `safety_car_changed` y `weather_changed`, emitidos solo al cambiar entre valores
    conocidos; los paquetes repetidos y los valores `unknown` no generan eventos duplicados. Un cambio de
    `session_uid` limpia el entorno, el pronóstico y los eventos de la sesión anterior.
-   Añadidas 9 pruebas a `tests/test_race_state.py` para B1, B2 y B3: valores reales de los fixtures,
    casos `None` por ceros restringidos o datos ausentes, transiciones de safety car y clima, no
    duplicación de eventos y reinicio completo por `session_uid`.
-   Validación local en Windows: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`
    y `uv run pytest` pasan; suite completa con 221 pruebas.
-   Los cambios de B1--B3 están en el árbol de trabajo de la rama `f2-wave1-wide-state`, sin commit. No se
    modificó el panel de consola y `progress.md` se actualiza con esta entrada.
-   Implementada la Ola 2 del histórico: C1 define los registros y snapshots inmutables; C2 cierra vueltas,
    muestrea valores de frontera y trata boxes, retiros y flashbacks; C3 incorpora tiempos oficiales,
    validez y stints desde `SessionHistory`, también si el paquete llega antes del cierre; C4 clasifica
    banderas de comparabilidad.
-   Revisada `plans/review_fase_2_ola_2.md` y aplicada C2b: se corrigieron edad de neumáticos, vueltas
    parciales/incompletas, cálculo de daño, flashback intra-vuelta, reinicio tras bandera roja y trabajo
    repetido sin cambios de frescura. Las banderas `partial_lap` e `incomplete` se propagan a clasificación
    y hacen que `is_clean` devuelva `False`.
-   Validación final local: 308 pruebas pasan; Ruff (lint y formato), Pyright y `git diff --check` pasan.
    La validación con captura real y CI queda pendiente.
-   Implementadas en paralelo las tareas D1--D9 como módulos independientes y registrados: progreso,
    combustible, neumáticos, ritmo, intervalos/rivales, boxes, ERS, daños y entorno. `GapTracker` aporta
    muestras de tendencia acotadas; D9 usa la temperatura media por vuelta conservada en `LapRecord`.
-   Validación local Windows de D1--D9: suite completa con 351 pruebas; Ruff, formato, Pyright y
    `git diff --check` pasan. Pendientes: integración E1--E4, validación/calibración con captura real y CI.

## 7. Hallazgos de la primera captura real

Captura: 68 541 paquetes, ~66 MB, 795,5 s entre el primer y el último paquete. Salida de `inspect`:

-   **Orígenes:** `192.168.68.59` (68 538 paquetes; es la IP de red local del propio PC del juego) y
    `127.0.0.1` (3 paquetes). Con Broadcast Mode el origen es la IP de red local, no loopback; si se usa
    `allowed_sources` en ese caso, debe llevar esa IP.
-   **Cabecera:** los 68 540 paquetes del juego llevan formato 2025, año 25, versión 1.26. El paquete
    restante (formato 30840, año 120) es la prueba manual `b'x' * 40`, no del juego.
-   **Tipos presentes:** 0 a 8, 10 a 13 y 15. Ausentes: 9 (`LobbyInfo`, solo online) y 14 (`TimeTrial`,
    solo contrarreloj). Hay un `FinalClassification`, así que la captura incluye el cierre de sesión.

| ID | Tipo | Paquetes | Frecuencia (según la especificación) |
| --- | --- | --- | --- |
| 0 | Motion | 8 674 | la del menú del juego (20 Hz) |
| 1 | Session | 869 | 2 Hz |
| 2 | LapData | 8 674 | la del menú del juego (20 Hz) |
| 3 | Event | 1 202 | cuando ocurre el evento |
| 4 | Participants | 88 | cada 5 s |
| 5 | CarSetups | 869 | 2 Hz |
| 6 | CarTelemetry | 8 674 | la del menú del juego (20 Hz) |
| 7 | CarStatus | 8 674 | la del menú del juego (20 Hz) |
| 8 | FinalClassification | 1 | una vez al final |
| 10 | CarDamage | 4 338 | 10 Hz |
| 11 | SessionHistory | 8 694 | 20 por segundo, rotando entre coches |
| 12 | TyreSets | 8 674 | 20 por segundo, rotando entre coches |
| 13 | MotionEx | 8 674 | la del menú del juego (20 Hz) |
| 15 | LapPositions | 435 | 1 Hz |

-   **Duración efectiva y frecuencia de envío.** Los tipos de frecuencia fija de la especificación permiten
    calibrar el reloj: `Session` (869 a 2 Hz), `CarSetups` (869 a 2 Hz), `CarDamage` (4 338 a 10 Hz),
    `LapPositions` (435 a 1 Hz) y `Participants` (88 cada 5 s) dan todos ≈ 434 s de emisión. De ahí se
    deduce que (a) la frecuencia de envío configurada en el juego es de 20 Hz (8 674 paquetes por fotograma
    en ~434 s) y (b) los 795,5 s incluyen ≈ 360 s sin datos. Causa de esos segundos sin datos: no
    confirmada (espera antes de entrar en sesión, pausas o los paquetes de prueba manuales).
-   **Tráfico:** ~155 KB/s (~1,3 Mbit/s) durante la emisión. Irrelevante para una red local, incluso por
    Wi-Fi. 20 Hz es suficiente para estrategia; no hace falta subir la frecuencia de envío.
-   **Corrección:** una versión anterior de este archivo anotaba ~11 Hz y ~85 KB/s, calculados sobre los
    795,5 s totales. Era un error.

### Especificación de referencia y puntos por verificar

-   **Fuente:** `F1_25_Telemetry_Output_Structures.txt` (estructuras C++) y `Data_Output_from_F1_25_v3.pdf`,
    documentación de EA para el formato 2025. Están disponibles como archivos del proyecto de Claude; no
    están en el repositorio (EA los facilita solo como referencia).
-   **Contrastado:** cabecera de 29 bytes, IDs 0 a 15 y `LapData` (57 bytes por coche; 22 coches más la
    cabecera y 2 bytes finales = 1 285 bytes, como declara la especificación).
-   **Verificado con la captura (2026-11-01):**
    1.  **Tamaños:** los 14 tipos presentes (0 a 8, 10 a 13 y 15) tienen exactamente el tamaño declarado
        en la especificación (por ejemplo, LapData 1285, CarDamage 1041, TyreSets 231). `Event` mide
        siempre 45 bytes. Esto sugiere que el 2026 Season Pack (v1.26) no cambió el tamaño de los
        paquetes del formato 2025; no prueba por sí solo que todos los campos coincidan.
    2.  **Orden de `m_gridPosition` y `m_driverStatus` en `LapData`:** el comentario de foro no se
        sostiene con esta versión. Con el orden de la especificación, el offset 43 toma 22 valores
        distintos (0 a 21) y constantes durante toda la captura, y el offset 44 vale 4 ("on track") en
        todos los coches. El offset 43 no puede ser `m_driverStatus`, cuyo rango es de 0 a 4. Se mantiene
        el orden de la especificación; el parser lo define en un único lugar y lo cubre con una prueba
        con fixtures derivados de datos reales.
-   **Base de `m_gridPosition`:** cruzando `LapData.m_gridPosition` con `m_carPosition` del primer
    `LapData` y con `m_gridPosition` de `FinalClassification` (offset 2 de cada coche; 46 bytes por coche
    tras la cabecera y `m_numCars`), el valor de `LapData` es siempre la parrilla menos 1 (base 0) para
    los 22 coches. `FinalClassification.m_gridPosition` y `m_carPosition` al inicio coinciden en base 1.
    La especificación no lo indica. El parser conserva el campo como `grid_position_zero_based`; el estado
    lo publica como `grid_position` base 1 para que coincida con `FinalClassification`.
-   **Fixtures reproducibles:** `tests/fixtures/` contiene un datagrama anonimizado por tipo implementado.
    Proceden de `captures/session-20261101-115103.f1cap` (SHA-256
    `5aef1a7c0d1eac85154221cf5f7b97af24bb06f3930219e68aa27bdc12a06e5f`); se limpiaron los nombres
    de participantes y los identificadores de red.
-   **Sesión grabada:** carrera de 5 vueltas con 22 coches (todos con `m_numLaps` = 5 en
    `FinalClassification`, y las posiciones finales forman una permutación de 1 a 22; esto valida también
    el layout de `FinalClassification`). Se desconoce si hubo paradas en boxes (`m_numPitStops`).
-   **Notas de la especificación que afectan al diseño:**
    -   `SessionHistory` y `TyreSets` se envían a 20 paquetes por segundo rotando entre coches: cada
        paquete es de un solo coche (`m_carIdx`), así que cada coche se actualiza alrededor de una vez por
        segundo, no a 20 Hz.
    -   Los índices de coche no cambian durante la sesión.
    -   Un coche tiene datos válidos si su `m_resultStatus` en `LapData` no es 0 (invalid) ni 1
        (inactive); `m_numActiveCars` de `Participants` indica cuántos coches hay.
    -   Con "Your Telemetry" en restringido (otros jugadores en multijugador), campos como combustible,
        ERS, daños y desgaste llegan a cero: deben tratarse como desconocidos, no como 0.
    -   F1 25 admite además los formatos 2024 y 2023; por ahora solo se prevé el 2025.
    -   El `.txt` y el PDF difieren en un tipo (`NEuint8` en `DRSDisabled` del `.txt`, `uint8` en el PDF);
        se interpreta como `uint8`.

### Captura de carrera con paradas y daños (2026-11-01)

-   Archivo local: `captures/session-20261101-233816.f1cap` (no se versiona). Contiene 174 948 paquetes,
    177 067 935 bytes de payload y 1 203,3 s; no está truncado y todos los paquetes tienen cabecera.
-   Los nueve tipos soportados suman 104 999 datagramas; el parser los procesó sin rechazos. La sesión es
    una carrera en Monza de 13 vueltas con 20 participantes activos y un paquete `FinalClassification`
    consistente con las 13 vueltas y 20 coches clasificados.
-   Cada uno de los 20 clasificados registra una parada y dos stints. En `Event` se observaron 39
    colisiones y 10 penalizaciones; son códigos observados, no una atribución causal de daños.
-   El piloto (índice 1, OCON) termina en posición 13 con una parada. Su `CarDamage` final indica 67 en
    ala delantera izquierda, 41 en daño de motor, 9 en caja de cambios y desgaste de neumáticos
    `(22, 16, 35, 12)`. El estado conserva esos valores brutos por coche; no calcula métricas derivadas.
-   Replay completo parser → estado: 22 slots de coche, 20 nombres activos y los valores finales de
    posición, vuelta, parada y daños concuerdan con `FinalClassification`/`CarDamage`.

## 8. Mapa del código

```text
src/f1_strategist/
  config.py       Configuración TOML validada (pydantic) y overrides de la CLI
  packets.py      Cabecera común de 29 bytes y nombres de tipos de paquete
  capture.py      Formato .f1cap v1: CaptureWriter, CaptureReader, CapturedPacket
  receiver.py     UdpReceiver (hilo + cola acotada), Stats y StatsSnapshot
  race_state.py   Estado de carrera de un solo escritor y snapshots inmutables
  session.py      CaptureSession: receptor -> cola -> consumidor (captura + estado + estadísticas)
  diagnostics.py  Estado de conexión y causas probables cuando no llegan datos
  replay.py       Reemisión de una captura por UDP respetando los tiempos
  summary.py      Resumen de una captura (comando inspect)
  console.py      Panel de consola con Rich (solo presentación)
  cli.py          Comandos run, replay e inspect
tests/            Pruebas unitarias y de extremo a extremo (replay -> receptor -> captura)
config/config.example.toml   Ejemplo de configuración
```

Comandos útiles:

```bash
uv sync                              # instalar dependencias
uv run pytest                        # pruebas
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run f1-strategist run --listen-address 0.0.0.0   # recibir y grabar
uv run f1-strategist inspect captures/<archivo>.f1cap
uv run f1-strategist replay captures/<archivo>.f1cap --host 127.0.0.1 --port 20777
```

## 9. Notas técnicas para agentes

-   Las capturas completas (`captures/`) no se versionan en git. Los fixtures compactos de la captura
    inicial sí se versionan en `tests/fixtures/`.
-   En Windows, usar PowerShell o Windows Terminal para la consola. Git Bash puede no redibujar el
    panel en vivo (hipótesis no confirmada).
-   `run` instala manejadores para Ctrl+C y SIGTERM: los procesos lanzados en segundo plano desde shells
    no interactivos ignoran SIGINT, y sin el manejador no se cerraba la captura limpiamente.
-   Si el juego envía por broadcast, la app debe escuchar en `0.0.0.0`; en loopback no recibe nada.
-   Otra aplicación de telemetría usando el puerto 20777 provoca un error claro al abrir el socket.

## 10. Siguiente paso: integración de la Fase 2

La Fase 2 está **en curso**. A1--A4, B1--B3, el histórico C1--C4/C2b y las métricas D1--D9 están
implementados. El siguiente bloque es:

1.  **Integración:** añadir `MetricsEngine` y snapshots; integrarlos en la sesión, la consola y la
    exportación JSON/CSV (E1--E4).
2.  **Validación y cierre:** recortar y anonimizar una captura, incorporar un fixture real cuando esté
    disponible y verificar las tolerancias, invariantes y documentación (F1--F4).

## 11. Pendientes y decisiones abiertas

-   Captura más variada: safety car y, si es posible, lluvia.
-   Probar la topología de red local con dos PC; decidir envío dirigido o broadcast y política de
    filtrado por IP de origen.
-   Esquema de SQLite; modelo local concreto del LLM; voz; framework del front-end de la GUI;
    empaquetado de escritorio; fórmulas de degradación y simulación de paradas; umbrales iniciales;
    idioma por defecto y cambio en caliente.
-   Registrar en `docs/` hallazgos adicionales de nuevas capturas y validación de CI.

### 2026-11-02

-   Corregidos los offsets y conteos de stints de `SessionHistory` y `FinalClassification`; los históricos
    ahora exponen solo los elementos declarados por sus contadores.
-   Añadidos fixtures de captura anonimizados, pruebas de layout con bytes no nulos, Hypothesis y un replay
    válido de extremo a extremo.
-   La versión menor 1.x se acepta con aviso cuando el resto de la cabecera y el tamaño coinciden; los
    tipos no implementados se ignoran sin generar rechazos.
-   Las métricas usan `overall_frame_identifier`, se calibran por cadencia observada y limitan las sesiones
    retenidas. Los errores y diagnósticos pasan a claves neutrales al idioma para renderizarse en consola.
