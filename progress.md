# Progreso del proyecto --- F1 Strategist

**Última actualización:** 2026-11-12
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
| Almacenamiento | Captura binaria cruda (`.f1cap`) + SQLite para derivados | Implementado; esquema actual v7 documentado en `docs/almacenamiento_sqlite.md` |
| Grabación e ingesta | Grabador en vivo con hilo y cola acotada; `ingest` offline idempotente | Implementado (G5); la cola del vivo no bloquea y la ingesta espera al escritor para no perder lotes |
| Replay y huella | Replay interno (`play`) y reemisión UDP (`replay`); huella canónica compartida por `verify` e `ingest` | Implementado (G2, G4, G5); la velocidad no cambia el tiempo simulado y la captura truncada se marca incompleta |
| Perfil del fin de semana | Se construye al empezar la carrera y se registra en la sesión (`profile_json` + `profile_hash`); la reproducción usa el registrado | Implementado (Fase 3.5, P1--P8); sin fin de semana enlazado no hay perfil y la captura continúa |
| Interfaz | Consola primero; GUI web local (FastAPI + WebSocket) después | Acordado |
| Topologías | Mismo PC o PC de la red local; solo cambia la configuración | Requisito del usuario |
| Seguridad de red | Escucha solo en loopback por defecto; apertura a la LAN explícita | Requisito |
| Receptor UDP | Hilo dedicado con socket bloqueante y cola acotada | Decisión cerrada; `overall_frame_identifier`, cadencia observada y retención limitada |
| Formato `.f1cap` v1 | Definido en `src/f1_strategist/capture.py` | Implementado en Sprint 1 |
| Parser | Registro de decodificadores `struct` precompilados; modelos inmutables | Nueve tipos; formato 2025, año 25, juego 1.x y paquete v1; otras versiones menores se avisan y requieren tamaño exacto |
| Estado de carrera | Un consumidor escritor publica snapshots inmutables | Sesión (entorno, pronóstico, safety car), coches (tiempos, intervalos, boxes, combustible/ERS, neumáticos), frescura por grupo y eventos. El motor de métricas integrado publica snapshots inmutables para consola y exportación |

## 4. Estado por fase del roadmap

| Fase | Estado | Notas |
| --- | --- | --- |
| 0. Definición y entorno | Parcial | Repo, CI definido, config, receptor y guía de firewall listos. Validada la recepción desde otro PC de la red local y en Linux real. |
| 1. Receptor, parser y estado | Implementada; CI verde | Snapshots inmutables de sesión y coches, daños, frescura por grupo, eventos y resumen de consola. |
| 2. Métricas e histórico | Implementada; validación parcial | A1--A4, B1--B3, C1--C4/C2b, D1--D9, E1--E4 y F2--F4 implementados. F3 y la documentación se actualizaron según O5; la semántica/tolerancia del MFD sigue abierta aunque la validación local actual pasa. |
| 3. Grabación y replay | Implementada; validación local completa | G1--G8 implementados sobre SQLite versionado (v7 tras las migraciones de Fase 4), `play`/`replay`, datasets, huella y `verify`, grabador en vivo e `ingest`, inspección con `sessions`, pruebas E2E y benchmarks. Documentación en `docs/almacenamiento_sqlite.md`. Queda repetir CI en Linux y macOS. |
| 3.5. Perfil del fin de semana | Implementada; validación local Windows | P1--P8 implementadas. Esquema SQLite v5, enlace de sesiones, resúmenes de tandas, inventario, perfil versionado y métricas `profile.*`; la prueba integrada cubre práctica, clasificación, carrera y reingesta con huella estable. Validación Linux por CI pendiente. |
| 4. Motor de estrategia | En curso; S1--S9, S12 y S13 implementadas | Ola 0 cerrada; S5--S8 cubren evaluación, decisión, motor integrado y registro SQLite; S9 añade panel y comandos. S13 incorpora prior por circuito y rango de reincorporación; S10 E2E/rendimiento y S11 documentación integral siguen pendientes. |
| 5--10 | No iniciadas | |

## 5. Qué está verificado y qué no

**Verificado:**

-   S13: el prior de circuito se evalúa después de las fuentes de sesión y conserva su fuente, banda y calidad;
    el cálculo publica rango de reincorporación. Validación Windows: `uv run pytest -q` (938 aprobadas,
    4 deseleccionadas), Ruff lint/formato, Pyright (0 errores), `git diff --check` y wheel con el CSV incluido.

-   La suite `pytest` (567 pruebas funcionales, 3 benchmarks excluidos por defecto), Ruff (lint y formato,
    96 archivos) y Pyright (0 errores) pasan en Windows (2026-11-08). Incluye las pruebas de captura real
    con el fixture de Monza y la prueba E2E de grabación. El wheel se probó desde un directorio externo
    al repositorio y carga el manifiesto empaquetado.
-   G7: `ingest` y `metrics` coinciden en vueltas, eventos y exportación JSON/JSONL/CSV; `play` a 0x, 1x y
    4x, con y sin pausas, conserva la misma huella que el runner offline; el extremo a extremo en vivo
    (replay UDP -> `CaptureSession`) guarda la sesión con sus metadatos y SHA de captura y coincide con
    `ingest` en vueltas y secuencia de tipos de evento.
-   Rendimiento: el benchmark de `observe` queda por debajo de 1 ms de media por paquete y un almacén lento
    simulado no bloquea al consumidor. La ingesta offline usa `block_when_full` para esperar al escritor en
    lugar de perder lotes.
-   Integridad de la base: `sessions export --out` rechaza sobrescribir la base o la captura asociada,
    incluidos enlaces duros; una captura truncada marca la sesión como incompleta; las pérdidas de lotes se
    atribuyen solo al UID afectado.
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
-   E1: `MetricsEngine` alimenta `LapTracker` e ingiere `SessionHistory`; publica `MetricsSnapshot`
    inmutables al cambiar de vuelta o sesión y como máximo cada `publish_interval_s`. Cada módulo se
    ejecuta aislado y un fallo produce una métrica `unavailable` sin detener al resto. El benchmark
    sintético de `apply` queda marcado y separado de la suite funcional.
-   E2: `CaptureSession` actualiza las métricas después de aplicar cada paquete al estado y expone
    `metrics_snapshot()` también en `SessionPort`. Un fallo del motor se registra como
    `metrics_update_error` sin afectar al consumidor, la captura ni el estado de carrera.
-   E3: panel `Métricas` con vueltas restantes, margen de combustible, tasa de desgaste, tendencia de
    ritmo e intervalos delante y detrás. Los ausentes se muestran como `—`; se distinguen las calidades
    y las filas dependientes de telemetría viva pasan a `stale` cuando `lap_data` envejece. Los fallos
    de módulo se muestran con su nombre y tipo de excepción.
-   E4: exportación JSON (`schema_version: 1`), JSON Lines y CSV, con unidades, ventanas y calidad por
    métrica; una fila por coche y vuelta en los formatos tabulares. El comando
    `f1-strategist metrics CAPTURA [--out ARCHIVO] [--format json|jsonl|csv]` usa el runner offline y
    está documentado en `docs/exportacion_metricas.md`.
-   Aplicadas las correcciones de `review_fase_2_ola_4.md` (O4-1--O4-10): protección contra
    sobrescribir la captura o la configuración, publicación final tras la ingesta oficial, inclusión
    de la vuelta final confirmada en ritmo, ausencia explícita de piloto, módulos con estado por motor,
    frescura efectiva en el panel, avisos por truncamiento/rechazos, diagnóstico visible de módulos
    fallidos, salida estándar UTF-8 y CSV con saltos de línea LF también en archivo.
-   Validación local Windows tras E1--E4 y la review: `uv run pytest -q` (401 pruebas funcionales y
    1 benchmark excluido), `uv run pytest -q -m benchmark` (1 prueba), `uv run ruff check .`,
    `uv run ruff format --check .` (77 archivos) y `uv run pyright` (0 errores/avisos) pasan.
-   Fase 3.5 (P1--P8), validada localmente en Windows. Cierra el ciclo completo del perfil del fin de
    semana: enlace de sesiones del juego o manual, inventario de neumáticos del piloto, resumen de tandas,
    persistencia en SQLite (esquema v5), perfil versionado y determinista, registro del perfil exacto al
    empezar la carrera, comando `weekend show` y métricas `profile.*` como prior de calidad `low`.
    -   P1: el enlace por identificadores del juego agrupa práctica, clasificación y carrera; el enlace
        manual exige un circuito común y lo rechaza si difieren. Migraciones desde v1 y v2 verificadas.
    -   P2: `RaceSessionSnapshot` publica el inventario completo de juegos del piloto (compuestos real y
        visual, desgaste, disponibilidad, vida útil, delta de vuelta y recomendación). Se limita al coche
        del piloto con telemetría no restringida y se reinicia al cambiar de sesión.
    -   P3/P4: `StintSummary` combina desgaste medido y ritmo con mediana ponderada por vueltas limpias;
        `min_representative_laps = 4`. Los resúmenes y el inventario `race_start`/`session_end` se
        guardan sin duplicados al reingerir.
    -   P5: el perfil es puro e inmutable; sin datos devuelve `unavailable` y nunca lanza. La huella es
        SHA-256 del JSON canónico e independiente del orden de entrada.
    -   P6: sin fin de semana enlazado no hay perfil y la captura sigue sin errores; un fallo de lectura
        se registra como `weekend_profile_error` y no detiene al consumidor; la reproducción reutiliza el
        perfil registrado en lugar de recalcularlo.
    -   P7: las siete métricas `profile.*` nunca superan calidad `low` y llevan la nota `profile_prior`;
        sin perfil son `unavailable`. El resto de módulos de métricas no cambia de comportamiento.
-   Validación Windows de la Fase 3.5: `uv run pytest -q` (662 aprobadas, 4 benchmarks excluidos),
    Ruff (lint y formato, 105 archivos), Pyright (0 errores) y `git diff --check` pasan, con rondas
    intermedias de 625 pruebas en P5, 646 en P6 y 659 en P7.
-   Fase 4, ola 0 (H1, H2 y S1--S4): los tipos del contrato (`strategy/types.py`) son inmutables y se
    serializan de forma canónica; `StrategyConfig` valida la sección `[strategy]` del TOML y existe el
    stub `strategy_snapshot()` en `CaptureSession` y `PlaybackSession`. Los modelos de ritmo, pérdida en
    boxes y neumáticos son puros, no inventan valores sin datos y declaran sus notas y calidades. Los 13
    hallazgos de `review_fase_4_ola_0.md` están corregidos con pruebas de regresión (detalle en la
    bitácora del 2026-11-11).
-   Validación Windows de la ola 0: `uv run pytest -q` (790 aprobadas, 4 benchmarks excluidos), Ruff
    (lint y formato, 116 archivos) y Pyright (0 errores) pasan. La suite partió de 768 pruebas antes de
    los fixes de la revisión.
-   S8: SQLite v6 migra desde v5 sin perder sesiones, guarda evaluaciones por vuelta y cambio de revisión
    en vivo y en `ingest`, y conserva una huella canónica de estrategia separada de `fingerprint`.
    La suite completa actual pasa (859 pruebas; 4 benchmarks excluidos), Ruff y Pyright (0 errores).
-   S9: SQLite v7 guarda el coche, compuesto y pronóstico de tiempos por vuelta. El panel de estrategia y
    `strategy show`, `export` (JSON/CSV) y `review` usan la lectura de sesiones; `review` compara el error
    absoluto medio por horizonte y omite datos históricos sin proyección. La lectura v6 permanece disponible.
    Verificación Windows: 878 pruebas aprobadas, 4 benchmarks excluidos; Ruff y Pyright (0 errores).
-   Revisión de la ola 1: se corrigieron H1, H2 y H4--H10 (identidad de juegos, línea base obligatoria,
    bloqueo por calidad/condiciones, cadencia, revisión de errores, proyección fraccionaria, vida de neumáticos,
    `review` y formato). H3 bloquea lluvia, SC/VSC y los eventos observables de bandera roja; la telemetría no
    expone un indicador directo y persistente de bandera roja activa, por lo que queda pendiente validación real.
    La reproducción de Suzuka ya termina correctamente con claves únicas. Validación Windows: 899 aprobadas,
    4 benchmarks excluidos; Ruff lint/formato y Pyright pasan.

**No verificado todavía:**

-   La calibración de los parámetros sin valor universal (capacidad de ERS, límite de desgaste y efecto
    del combustible en el ritmo) queda para una fase posterior; las métricas dependientes siguen
    `unavailable` hasta configurarlos.
-   El fixture real de F3 no incluye lluvia ni safety car; las tolerancias bajo esas condiciones siguen
    sin verificar.
-   La Fase 3.5 solo está validada en Windows: CI en Linux y macOS queda pendiente (Docker no tiene
    daemon disponible y WSL solo muestra la distribución interna `docker-desktop`). El perfil del fin de
    semana tampoco se ha probado todavía contra capturas reales de un fin de semana completo.
-   La Fase 4 aún requiere validar el motor con capturas reales y completar E2E/rendimiento de S10. La nueva
    captura de Sakhir contiene alta degradación y safety car, pero las observaciones del HUD no identifican
    por sí solas la causa de los datos faltantes ni validan las decisiones del motor; tampoco se ha medido
    el requisito de rendimiento de S7 (<1 ms medio).
-   Queda volver a ejecutar CI de GitHub Actions después de la Ola 5.

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

### 2026-11-04

-   Implementada E1: `MetricsEngine` alimenta `LapTracker`, ingiere `SessionHistory` y publica
    `MetricsSnapshot` inmutables al cambiar de vuelta o sesión y como máximo cada `publish_interval_s`.
    Cada motor crea sus propios módulos, los fallos se aíslan por módulo y el benchmark de `apply` queda
    marcado y separado de la suite funcional.
-   Implementada E2: `CaptureSession` actualiza las métricas después de aplicar el estado y expone
    `metrics_snapshot()` también en `SessionPort`. Un fallo del motor se registra como
    `metrics_update_error` sin afectar al consumidor, la captura ni el estado de carrera.
-   Implementada E3: panel `Métricas` con vueltas restantes, margen de combustible, tasa de desgaste,
    tendencia de ritmo e intervalos delante y detrás. Los datos ausentes se muestran como `—`, las
    calidades se distinguen y las métricas de telemetría viva pasan a `stale` cuando `lap_data` envejece.
    Los fallos de módulo se muestran con su nombre y tipo de excepción.
-   Implementada E4: exportación JSON (`schema_version: 1`), JSON Lines y CSV con unidades, ventanas y
    calidad por métrica; una fila por coche y vuelta en los formatos tabulares. El comando
    `f1-strategist metrics CAPTURA [--out ARCHIVO] [--format json|jsonl|csv]` usa el runner offline y
    está documentado en `docs/exportacion_metricas.md`. La salida estándar es UTF-8 y los archivos CSV
    conservan saltos de línea LF.
-   Aplicadas las correcciones de `review_fase_2_ola_4.md` (O4-1--O4-10): protección contra sobrescribir
    la captura o la configuración, publicación final tras la ingesta oficial, inclusión de la vuelta
    final confirmada en ritmo, ausencia explícita de piloto, módulos con estado por motor, frescura
    efectiva en el panel, avisos por truncamiento o rechazos del parser y diagnóstico visible de módulos
    fallidos.
-   Validación local Windows: `uv run pytest -q` (401 pruebas funcionales y 1 benchmark excluido),
    `uv run pytest -q -m benchmark` (1 prueba), `uv run ruff check .`,
    `uv run ruff format --check .` (77 archivos) y `uv run pyright` (0 errores/avisos) pasan.
-   Los cambios de E1--E4 y de la review están en el árbol de trabajo de la rama
    `f2-wave-4-integration`, sin commit. El panel de estado de carrera existente no cambia.

-   Implementada F2: se incorporó `monza-13-laps.f1cap.gz` como fixture real de 13 vueltas y 5,4 MB,
    anonimizado y comprimido. El archivo no se versiona por superar el límite acordado; las pruebas se
    omiten cuando no está disponible o no se define `F1_CAPTURE_FIXTURE`.
-   Implementada F3: `tests/test_metrics_real_capture.py` reproduce la captura completa (36 471 paquetes,
    sin truncamiento ni rechazos) y verifica 20 coches × 13 vueltas, tiempos y sectores oficiales,
    mejor vuelta, paradas, edad y desgaste de neumáticos, combustible, intervalos, vueltas restantes y
    tendencias. El coste medio de `MetricsEngine.apply` fue de 0,225 ms, por debajo de 1 ms.
-   Corregido un falso `flashback` en la vuelta final: `_is_in_lap_flashback` ignora los coches con
    `result_status="finished"` para no confundir el reinicio del cronómetro tras la meta con un replay.
-   Implementada F4: actualizadas la sección 3.4 y el ejemplo de snapshot del outline, el criterio de
    salida medido del roadmap y este estado de la fase. Las 9 pruebas `real_capture` pasan contra el
    fixture real; la suite completa queda en 410 pruebas funcionales y 1 benchmark excluido, con Ruff
    (78 archivos), Pyright y `git diff --check` correctos.
-   Los cambios de F2--F4 están en la rama `f2-wave-5-validation`; el fixture real permanece fuera de git.

### 2026-11-05

-   Aplicadas las correcciones pertinentes del review O5: un tiempo oficial ya no oculta cobertura
    parcial; combustible segmenta primero los stints y usa un fallback explícito de un solo stint
    anterior; `pace.best_lap_s` representa la mejor vuelta oficial válida, incluso con tráfico.
-   F3 ahora falla ante una ruta de captura explícita inexistente y omite solo el fixture opcional
    no solicitado. La validación exige muestras crudas suficientes y compara extremos por vuelta de
    `CarStatus`/`CarDamage` con el histórico, además de revisar progreso, paradas, intervalos y
    desviación de combustible durante las publicaciones.
-   `fuel.game_deviation_laps` compara el margen estimado al final con el valor `fuel_remaining_laps`
    del MFD. La semántica del protocolo sigue sin especificar más que “valor en el MFD”; la tolerancia
    de ±0,3 vueltas no se declara cumplida.
-   El detector de saltos conserva causas y reinicia continuidad cuando falta una muestra válida.
    F3 aplica umbrales a ritmo y desgaste sin causa y verifica disponibilidad/atribución para
    tendencias de intervalo, sin imponerles un umbral numérico.
-   El ejemplo de snapshot del outline queda identificado como ilustrativo y refleja el esquema
    medido de `pace.last_lap_s` y `tyres.wear_rate_pct_per_lap`; el roadmap limita las afirmaciones a
    lo comprobado en el fixture reducido.
-   Validación final local: 421 pruebas pasan y 1 benchmark queda excluido por defecto; el benchmark
    pasa al ejecutarse con `-m benchmark` y las 15 pruebas de `real_capture` pasan con el fixture local.
    Ruff (archivos Python afectados), Pyright y `git diff --check` pasan. CI remota no se ejecutó.

### 2026-11-06

-   Aplicados los nueve hallazgos de `review.md` de la Ola 0: la huella conserva todos los eventos,
    incluye tiempos simulados y configuración completa; `verify`, `datasets verify` y `play` rechazan
    capturas truncadas, con rechazos del parser o sin paquetes válidos.
-   El reloj de `play` avanza también ante paquetes ignorados/rechazados; buscar una vuelta ausente
    falla claramente y no reintenta en bucle. El catálogo predeterminado se resuelve sin depender del
    directorio actual y su manifiesto está incluido en el wheel; las capturas siguen siendo opcionales.
-   SQLite valida versión y tablas en solo lectura, y no cambia journal mode antes de rechazar una
    base futura; se registraron estos contratos y resultados en esta bitácora.
-   Validación local: 488 pruebas pasan, 1 benchmark se excluye por defecto, Ruff (lint/formato),
    Pyright y `git diff --check` pasan. El wheel se construyó y su comando de catálogo se probó desde
    fuera del repositorio. CI remota no se ejecutó.

### 2026-11-07

-   Endurecida la ingesta de stints de `SessionHistory`: se valida la secuencia antes de reemplazarla;
    ante datos incoherentes se conserva el último historial válido, pero se siguen ingiriendo tiempos
    oficiales disponibles. El motor registra una advertencia por sesión/coche/causa, no una por paquete.
-   `RaceState` aplica `RTMT` al coche indicado una sola vez, acepta con UID 0 solo una clasificación
    compatible con el índice y las vueltas del jugador activo, conserva resultados terminales frente a
    `LapData` activo tardío y no convierte esos resultados en fin global de sesión. `SEND` seguido por
    `SSTA` con el mismo UID conserva el fin observado.
-   Los eventos de boxes incluyen salida; `RaceState` y `LapTracker` evitan inferir transiciones de
    boxes para coches en estado terminal. La consola muestra el resultado del piloto y el motivo crudo
    comunicado por el juego sin inferir una causa de retirada.
-   Pace y desgaste anotan el mínimo de muestras aplicable; la consola muestra `m/n muestras válidas`
    para valores insuficientes. Se añadieron regresiones para secuencias inválidas de stints, recuperación,
    UID 0, retiro, fin de sesión, boxes y 0–3 muestras con umbrales configurados.
-   Validación local: 560 pruebas pasan y 1 benchmark queda excluido; las 15 pruebas de captura real
    pasan y `f1-strategist metrics` termina correctamente con el fixture local de Monza. Ruff (lint y
    formato, 92 archivos), Pyright y `git diff --check` pasan. La captura original de Baku del 7 de
    noviembre no está disponible en este workspace, por lo que estos cambios no se validaron contra ella.
    CI remota no se ejecutó.

### 2026-11-08

-   Implementada G5: `SessionRecorder` escribe desde un hilo propio con cola acotada, separa los metadatos
    del productor y del escritor, atribuye las pérdidas de lotes por `session_uid`, conserva eventos
    idénticos consecutivos mediante alineación ordenada y muestrea métricas al cambiar el valor o la calidad,
    al cerrar cada vuelta y al cerrar la sesión. Guarda `t_sim_s` (captura) y `t_session_s` (cabecera) por
    separado y propaga una sola vez los errores y pérdidas del grabador a las estadísticas.
-   Implementada la ingesta offline: `ingest` reutiliza el pipeline y el grabador, comparte con `verify` la
    huella canónica, marca incompletas las capturas truncadas, procesa antes de reemplazar y detiene el
    grabador también cuando hay error. La opción `block_when_full` evita perder lotes cuando el escritor va
    por detrás; el vivo mantiene el encolado no bloqueante.
-   Implementada G6: el comando `sessions` ofrece `list`, `show`, `laps`, `events`, `metrics` y `export`.
    Las consultas abren la base en solo lectura, `held_metric_values` arrastra el último valor sin extrapolar
    a `total_laps` y `sessions export --format json` reproduce exactamente la instantánea final de `metrics`;
    `jsonl` y `csv` reproducen las vueltas.
-   Aplicados los 15 hallazgos de `review.md`: esquema v2 con `final_metrics_json`, `update_session` con
    `COALESCE`, huella de ingesta igual a la de `verify`, export JSON exacto, protección de la base y de la
    captura asociada frente a enlaces duros, metadatos tardíos fusionados, relojes separados, muestreo final
    forzado, pérdidas por UID, reemplazo atómico, captura truncada incompleta, idempotencia estricta y
    deduplicación de eventos que conserva la multiplicidad.
-   Implementada G7: `tests/test_recording_e2e.py` valida `ingest` frente a `metrics`, la huella de `play` a
    0x/1x/4x con y sin pausas, el flujo en vivo por replay UDP y los benchmarks de `observe` y almacén lento.
    El fixture real de Monza se usa con el marcador `real_capture`.
-   Implementada G8: creado `docs/almacenamiento_sqlite.md` con el esquema v2, la semántica de
    `metric_samples`, consultas de ejemplo, regeneración desde una captura y el procedimiento para añadir
    una migración; actualizadas la sección 3.11 del outline, la Fase 3 del roadmap y este archivo.
-   Validación local Windows: `uv run pytest -q` (567 pruebas y 3 benchmarks deseleccionados),
    `uv run pytest -q -m benchmark` (2 pruebas E2E), Ruff (lint y formato, 96 archivos) y Pyright
    (0 errores) pasan. La CI remota en Linux y macOS queda pendiente. Los cambios de G8 están en el árbol
    de trabajo, sin commit.
-   Se añadieron al catálogo dos recortes anonimizados de sesiones de carrera extraídas por UID de
    capturas que contenían práctica, clasificación y carrera: Baku (13 vueltas, VSC, parada y DNF;
    2 733 874 bytes) y Melbourne (20 vueltas, una parada del piloto y resultado finalizado;
    9 303 462 bytes). Las capturas originales permanecen locales y no se versionan.
-   El manifiesto registra hashes, tamaños, expectativas y etiquetas de escenario. `datasets verify`
    validó los tres escenarios (incluido Monza), las 15 pruebas de `test_datasets.py` pasan y Ruff,
    formato y `git diff --check` pasan.
-   Cerrada la persistencia multisesión: al cambiar el UID se finaliza la instantánea de métricas de la
    sesión anterior; tras cerrar el archivo, el SHA-256 final se asigna a todas las sesiones creadas por
    esa captura. Las pruebas E2E confirman que cada export coincide con el segmento aislado y que
    `ingest --replace` reemplaza el grupo completo.
-   Se midió el consumidor UDP con un almacén SQLite normal y otro que retrasa 20 ms cada transacción,
    sobre el mismo escenario sintético de 27 paquetes. En esta ejecución se recibieron los 27 paquetes
    en ambos casos, sin pérdidas UDP ni lotes descartados; el máximo observado entre recepción y entrada
    al consumidor fue 0,42 ms (normal) y 5,71 ms (lento), con picos de cola de 2 y 20 paquetes. Son
    resultados de este escenario, no un umbral general de rendimiento.
-   Validación tras estos cambios: `uv run pytest -q` (571 aprobadas, 4 benchmarks excluidos),
    `uv run pytest -q -m benchmark` (4 aprobadas), Ruff (96 archivos), Pyright (0 errores) y
    `git diff --check` pasan. La fixture de Monza permanece local y sin seguimiento.

### 2026-11-09

Detalle de las tareas P1--P8 de la Fase 3.5, en el orden en que se cerraron. La suite creció de
625 pruebas (tras P5) a 646 (P6), 659 (P7) y 662 (P8), siempre con Ruff y Pyright limpios.

-   **P1 --- Enlace de sesiones y comando `weekends`** (`7b94e40`). Esquema SQLite v3: columnas
    `season_link_id`, `weekend_link_id`, `session_link_id`, `weekend_structure_json` y `weekend_id`
    en `sessions`, más la tabla `weekends` con origen `game_ids` o `manual`. El grabador persiste los
    identificadores del juego y la estructura del fin de semana; un enlace distinto de cero crea o
    reutiliza el fin de semana, y la metadata tardía se fusiona. Los identificadores `<= 0` se
    normalizan a `None`. Comandos `weekends list|show|link|unlink`; el enlace manual agrupa sesiones
    por circuito y rechaza mezclar circuitos distintos. Servicios de consulta y pruebas de migración
    desde v1 y v2 incluidas.
-   **P2 --- Inventario completo de neumáticos del piloto** (`a312732`). `RaceSessionSnapshot` publica
    `tyre_inventory` (tupla de `TyreSetSnapshot`) y `tyre_inventory_updated_at_monotonic`. Cada juego
    lleva compuesto real y visual, desgaste, disponibilidad, vida útil, delta de vuelta, sesión
    recomendada y si está montado. Solo se publica el coche del piloto y solo con telemetría no
    restringida; el inventario se reinicia al cambiar de sesión. La consola no cambia.
-   **P3 --- Resumen de tandas** (`a606172`). Nuevo módulo puro `metrics/stint_summary.py` con
    `summarize_stints(laps, stints, config)` y el modelo `StintSummary` por coche y tanda. El desgaste
    medido es la señal principal; el ritmo se informa en bruto y solo se corrige por combustible si
    `fuel_effect_s_per_kg` está configurado, sin inventar la corrección. Los valores se combinan con
    mediana ponderada por vueltas limpias. `MetricsConfig.min_representative_laps = 4` y 17 pruebas.
-   **P4 --- Persistencia de tandas e inventario** (`699bb76`). Esquema SQLite v4 con las tablas
    `stint_summaries` y `tyre_inventory_snapshots`, cuyas claves evitan duplicados al reescribir.
    `SessionStore` añade `add_stint_summaries`, `add_tyre_inventory`, `iter_stint_summaries` e
    `iter_tyre_inventory`. El grabador guarda los resúmenes al cerrar cada sesión y el inventario en
    `race_start` (al empezar la carrera) y `session_end`; la ingesta offline hace lo mismo.
-   **P5 --- Perfil del fin de semana** (`a30267b`). Nuevo `weekend_profile.py` con `WeekendSession`,
    `ProfileValue`, `CompoundProfile`, `WeekendProfile`, `build_weekend_profile`, `blend_with_prior`,
    `profile_to_json` y `profile_hash`. El perfil es inmutable y versionado (`schema_version = 1`);
    agrega por compuesto con mediana ponderada por vueltas limpias y elige como referencia automática
    el seco más blando con tandas representativas (forzable por parámetro). La mezcla con el prior usa
    `k = 3`; la huella es SHA-256 del JSON canónico y no depende del orden de entrada. Sin datos
    devuelve un perfil vacío `unavailable`, nunca lanza. 11 pruebas.
-   **P6 --- Proveedor del perfil y comando `weekend show`** (`245d304`). Esquema SQLite v5 con
    `sessions.profile_json` y `profile_hash`, que guardan el perfil exacto usado al arrancar la carrera.
    Nuevo `weekend_provider.py`: localiza el fin de semana por `weekend_id`, por los identificadores del
    juego o por enlace manual del mismo circuito (solo si es único), lee resúmenes e inventario y
    construye el perfil; nunca lanza, devuelve `error`. `SessionRecorder` gana `set_weekend_profile` y
    un perfil por defecto para la ingesta; `CaptureSession` resuelve el perfil una vez por UID de
    carrera, lo expone y se lo entrega al grabador, y un fallo de lectura se registra como
    `weekend_profile_error` sin detener la captura. `ingest` reutiliza el perfil registrado de la misma
    captura en lugar de recalcularlo. Comando `f1-strategist weekend show [--session ID]`, que muestra
    el perfil «registrado al empezar la carrera» o lo calcula y lo rotula «calculado ahora (no
    registrado)». 21 pruebas nuevas.
-   **P7 --- Métricas `profile.*`** (`b0dee82`). Nuevo `metrics/profile.py` con siete métricas:
    desgaste (`profile.wear_rate_pct_per_lap`), ritmo medio, delta contra la referencia, consumo,
    pérdida en boxes, juegos disponibles y confianza. Todas llevan calidad máxima `low` y la nota
    `profile_prior`: son un prior, no una medición. Sin perfil son `unavailable` y nada falla; si el
    compuesto montado no está en el perfil se usa la referencia y, sin ella, la métrica queda
    `unavailable`. `MetricInputs` recibe `profile` (import diferido para no cerrar un ciclo con
    `store`), `MetricsEngine` expone `profile` y `set_profile()`, el registro incluye `ProfileMetrics`
    y `CaptureSession` entrega el perfil al motor. La exportación JSON ya reflejaba las claves nuevas
    sin cambios. 13 pruebas.
-   **P8 --- Validación y documentación** (`9247ef2`). `tests/scenarios.py` genera un fin de semana
    sintético con práctica, clasificación y carrera, con circuito y enlaces de juego compartidos, UID
    distinto por sesión y reloj observado monótono. La prueba E2E graba y cierra las sesiones previas,
    comprueba los resúmenes por compuesto, registra el perfil al empezar la carrera y reingiere la
    captura de carrera con el mismo perfil y la misma huella. Además:
    -   La prueba integrada detectó que el grabador retenía una referencia mutable a `LapHistory`; el
        motor reiniciaba ese objeto al cambiar de UID antes de persistir el resumen anterior. El grabador
        ahora retiene el snapshot inmutable, y la prueba confirma que práctica y clasificación guardan sus
        tandas.
    -   Añadido `profile_from_json()` con verificación de versión y round-trip estable. `ingest` valida el
        hash del perfil registrado y lo aplica al motor solo en sesiones de carrera, de modo que la
        reproducción de esa captura conserva las métricas `profile.*`; los perfiles JSON incompletos o con
        hash inválido fallan explícitamente.
    -   Documentado el esquema SQLite v5 y las tablas de fines de semana, resúmenes e inventario;
        actualizados el outline, el roadmap de Fase 3.5 y el estado de P8 en el Anexo B.
    -   Validación Windows: `uv run pytest -q` (662 aprobadas, 4 benchmarks excluidos), Ruff (lint y
        formato, 105 archivos), Pyright (0 errores) y `git diff --check` pasan. Linux queda pendiente:
        Docker no tiene daemon disponible y WSL solo muestra la distribución interna `docker-desktop`.

### 2026-11-11

Ola 0 de la Fase 4 (motor de estrategia), cerrada en la rama `f4-wave-0-baseline` con siete commits:
H1 (`6aceba9`), H2 (`c396cd7`), S1 (`b86cbca`), S2 (`7c49356`), S3 (`b2c9ed0`), S4 (`a473d4d`) y los
fixes de la revisión (`0b3bd76`).

-   **H1 --- Reconciliar compuesto por vuelta y límites de tanda** (`6aceba9`). Cada vuelta se asigna a
    su tanda por el compuesto y la edad **observados al cerrar la vuelta**; los límites declarados de
    `SessionHistory` quedan como respaldo cuando falta el dato observado. Se documentó la causa del
    desfase de una vuelta observado en el informe de Suzuka (práctica y carrera). Toca
    `metrics/history_ingest.py` y `metrics/stint_summary.py`, con pruebas del desfase sintético,
    tandas sin cambio de compuesto, dos paradas y `SessionHistory` tardío.
-   **H2 --- Primera vuelta rápida tras boxes y `first_lap`** (`c396cd7`). El cruce de la línea de meta
    al salir de boxes (caída de distancia desde ~la longitud del circuito hasta casi cero con reinicio
    del tiempo de vuelta) ya no se clasifica como *flashback*: la vuelta rápida de 96,099 s conserva su
    tiempo oficial y sigue marcada como salida de boxes. El tramo de salida se separa del cronometrado y
    `first_lap` se limita al inicio de una carrera. Toca `metrics/comparability.py` y
    `metrics/lap_tracker.py`.
-   **S1 --- Tipos, configuración y esqueleto del motor** (`b86cbca`). Nuevo `strategy/types.py` con el
    contrato inmutable de acciones, alternativas, evaluación y snapshot, serialización JSON canónica
    (`schema_version`, `inputs_hash` estable) y constantes de supuestos y riesgos. `StrategyConfig` en
    `config.py` con validación, anulaciones por circuito y sección `[strategy]` del TOML de ejemplo;
    stub `strategy_snapshot()` en `SessionPort`, `CaptureSession` y `PlaybackSession`; grupo
    `f1-strategist strategy` en la CLI. Decisión D3: `RecordingConfig.enabled` pasa a `True` por
    defecto y las pruebas que no quieren escribir en disco lo desactivan explícitamente.
-   **S2 --- Modelo de ritmo y desgaste por compuesto** (`7c49356`). `strategy/pace_model.py` con
    `build_compound_models` y `project_lap_times`: regresión sobre vueltas limpias del stint actual,
    mezcla con el prior del fin de semana por número de muestras, compuestos no probados por delta de
    ritmo del perfil o del juego y juego usado por desgaste inicial. Cada modelo publica `source`,
    muestras, calidad y notas; sin datos no hay modelo.
-   **S3 --- Pérdida en boxes y reincorporación** (`b2c9ed0`). `strategy/pit_model.py` con
    `estimate_pit_loss` (orden de fuentes: anulación de configuración, mediana del campo, prior del fin
    de semana y carril de boxes; sin fuente queda `none`, nunca 0) y `estimate_rejoin` (posición,
    coches cercanos, `traffic_risk` y contraste con `pit_rejoin_position` del juego), con dispersión
    robusta y banda. Declara explícitamente el supuesto de que los rivales no paran.
-   **S4 --- Inventario y reglas de neumáticos** (`a473d4d`). `strategy/tyre_options.py` con
    `tyre_options`: juegos elegibles por compuesto y vida útil, compuestos secos ya usados, obligación
    por la regla de dos compuestos (o pendiente si no puede evaluarse) y aviso de lluvia. Sin inventario
    devuelve «sin opciones» con nota explícita; intermedios y mojado quedan fuera de alcance.
-   **Fixes de `review_fase_4_ola_0.md`** (`0b3bd76`), 13 hallazgos sin cambios de contrato:
    -   H01/H06 (`tyre_options`): solo se ofrecen juegos con disponibilidad confirmada (`is True`); un
        `None` produce la nota `inventory_availability_unknown` y, si no queda nada, calidad
        insuficiente. El horizonte del pronóstico se deriva de las vueltas de la ventana de parada y el
        ritmo del jugador (respaldo de 30 min con `forecast_horizon_assumed`; ritmo sin filtrar con
        `forecast_lap_time_approximate`).
    -   H02/H10/H13 (`types`): `set_index` es índice cero-based; el hash de entradas admite cualquier
        `Mapping` (incluidos los `MappingProxyType` del histórico) y no depende del orden; supuestos,
        riesgos y sensibilidades se canonizan como conjuntos, las alternativas se ordenan por clave con
        validación de duplicados y los tipos serializables ganan `from_dict`/`from_json`.
    -   H03/H04 (`pace_model`): un compuesto sin vueltas de carrera se ancla siempre al ritmo de carrera
        ancla más el delta (nunca al ritmo absoluto de práctica) y el desgaste se mezcla con el prior
        (`wear_blended_with_prior`); un modelo solo-prior queda no proyectable en lugar de inventar
        pendiente.
    -   H05/H12 (`pit_model`): `did_not_finish`, `disqualified` y `not_classified` excluyen del cálculo
        de reincorporación; las muestras negativas de pérdida se descartan con nota
        (`negative_pit_loss_samples_discarded`) y el estimador cae a perfil o carril en lugar de fallar.
    -   H07/H08/H11 (`lap_tracker`): el cruce de meta exige salida de boxes (sin ella el retroceso es
        *flashback*, que ya no se descarta); la evidencia de la vuelta de salida se conserva en el
        registro único por vuelta; `first_lap` cubre `race`, `race_2` y `race_3`, con reconciliación si
        el tipo de sesión llega tarde.
    -   H09 (`stint_summary`): sin tandas declaradas se resumen las observaciones (calidad `low`) y una
        edad de neumático repetida ya no fragmenta la tanda; solo un descenso real de edad abre una
        nueva.
-   Validación local Windows del cierre: `uv run pytest -q` (790 aprobadas, 4 benchmarks excluidos),
    Ruff (lint y formato, 116 archivos) y Pyright (0 errores) pasan. La suite partió de 768 pruebas
    antes de los fixes (+22 nuevas). `git diff --check` limpio. CI remota no se ejecutó.

### 2026-11-12

**S8 --- Registro de evaluaciones en SQLite, esquema v6.**

-   Migración v6 añade `sessions.strategy_fingerprint` y `strategy_evaluations`, conservando las bases
    v5 y sus datos. El almacén expone escritura idempotente por `(session_id, seq)` y lectura ordenada.
-   `SessionRecorder.record_strategy` encola evaluaciones sin acceso síncrono a SQLite; persiste una por
    vuelta y cada cambio de revisión. La sesión en vivo y la ingesta offline usan la misma ruta.
-   La huella independiente cubre la configuración de estrategia en JSON canónico y no depende del
    orden de claves ni del sistema operativo; la huella existente de captura no cambia.
-   Verificación Windows: 859 pruebas aprobadas, 4 benchmarks excluidos; Ruff y Pyright (0 errores).

**UE1 y UE3 --- fixtures reales y regla de neumáticos.**

-   UE1: se añadieron los fixtures anonimizados de práctica y carrera de Suzuka al manifiesto; los cinco
    escenarios del manifiesto se verifican correctamente.
-   UE3: `tyre_options` exige en carreras normales secas dos compuestos distintos, con al menos un juego
    visualmente medio o duro. La regla no se impone con lluvia ni en Sprint; una sesión `race` sin
    `weekend_structure` queda sin clasificar y no activa la regla prematuramente.
-   La captura de China confirma el Sprint por `short_sprint_shootout` en `weekend_structure` (`race`) y la
    carrera principal como `race_2`. En algunos paquetes de esta última, la telemetría registra ventana
    ideal/límite 9/17; queda pendiente confirmar visualmente el texto del HUD.
-   Validación: 866 pruebas aprobadas, 4 benchmarks excluidos; pruebas de neumáticos (31), Ruff y Pyright
    pasan.

**S9 --- Panel de consola y comandos `strategy`, esquema v7.**

-   El panel y `strategy show` presentan la recomendación, confianza, ganancia, supuestos, riesgos y calidad;
    `strategy export` admite JSON/CSV y `strategy review` calcula el error absoluto medio por horizonte.
-   SQLite v7 conserva el coche, compuesto y tiempos proyectados por vuelta para comparar contra vueltas
    reales válidas posteriores. Las bases v6 siguen siendo consultables, pero sus evaluaciones sin pronóstico
    no contribuyen a `review`.
-   Verificación Windows inicial: 878 pruebas aprobadas, 4 benchmarks excluidos; Ruff y Pyright (0 errores).

**Revisión de la ola 1 --- correcciones posteriores a S9.**

-   H1: las alternativas y recomendaciones identifican el juego de neumáticos; se comprobó la reproducción
    original sobre Suzuka, que ahora emite JSON con código 0 y alternativas únicas.
-   H2: la línea base obligatoria usa la ventana ideal o, si no está disponible, la última parada viable;
    una parada obligatoria se recomienda aunque no haya ganancia robusta frente a ese plan.
-   H3: lluvia actual/pronosticada y SC/VSC bloquean recomendaciones. Los eventos observables de bandera roja
    mantienen el bloqueo hasta cerrar una vuelta normal tras la reanudación; la detección en vivo requiere una
    captura real, pues el protocolo no publica una señal directa de bandera roja activa.
-   H4--H8: se incorporan calidad e identidad del juego al contrato de evaluación v3; el motor reevalúa cambios
    de historial, inventario, distancia y frescura, reintenta fallos con cadencia mínima e incrementa revisión
    al entrar/salir de error. Las fracciones se proyectan cronológicamente y el riesgo de vida se limita al plan
    seleccionado y considera ambos juegos.
-   H9--H10: `review` omite vueltas inválidas/ausentes sin truncar horizontes posteriores, pero se detiene ante
    paradas o cambio de compuesto; el formato completo exigido por CI pasa.
-   Validación Windows: 899 pruebas aprobadas, 4 benchmarks excluidos; Ruff lint/formato y Pyright (0 errores).

**UE2 parcial --- captura real de Sakhir.**

-   La captura `captures/session-20261112-162235.f1cap` incluye práctica 1, clasificación corta y carrera de
    20 vueltas. Se añadieron al manifiesto los recortes anonimizados de práctica y carrera; la carrera conserva
    el evento de safety car y señales de alta degradación. Informe: `docs/example/informe_sakhir_20261112.md`.
-   El piloto inició con C3 blando, paró en la vuelta 9 y montó C1 duro; el desgaste medio registrado al
    cierre de la vuelta 8 fue 46,25 %. El juego mostró dos planes (blando→medio, parada en 10; blando→duro,
    parada en 9). Falta grabar el caso opuesto donde mantener sea claramente mejor.
-   Observaciones del HUD: predominó «datos insuficientes», «mantener» apareció unas cuatro vueltas desde
    aproximadamente la 10 y luego volvió la falta de datos; la reincorporación desapareció cerca de dos vueltas
    tras las paradas. Compuesto, juego, vuelta, ganancia contra la línea base e intervalo de ganancia quedaron
    como «—». Causa no determinada; no se atribuye al motor sin registro de la evaluación mostrada.
-   El safety car se desplegó en la vuelta 17 y se retiró en la 18; su modelado sigue fuera del MVP y debe
    priorizarse después. Se registra también investigar un prior externo de posición de reincorporación cuando
    aún nadie haya parado, refinable a cálculo con huecos/posiciones en vivo.

**S13 --- Prior de pérdida en boxes y reincorporación por circuito.**

-   El CSV se carga como recurso empaquetado. El prior es el último recurso tras anulación, muestras de carrera,
    perfil del fin de semana y tiempo de carril. El año de calendario no se usa como filtro; se excluye Sepang
    porque no está disponible en F1 25 ni en su DLC 2026.
-   SC/VSC usan sus columnas específicas; se conserva ±0,5 s para SC solo donde la fuente lo indica. El HUD y
    la evaluación muestran fuente, banda, calidad y la estimación/rango de posición; la neutralización sigue
    bloqueando la recomendación. Sin circuito o valor aplicable, el resultado queda no disponible.
-   La captura de Madring proporcionada confirma `track_id=42` en 1.089 paquetes de sesión; el parser ahora lo
    expone como `madring` y el prior real observado es 31,1 s. La fila advierte que no es una cifra oficial.
-   Validación Windows: 938 pruebas aprobadas, 4 deseleccionadas; Ruff lint/formato, Pyright (0 errores),
    `git diff --check` y wheel construido con el CSV incluido.

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
  session.py      CaptureSession: receptor -> cola -> consumidor (captura, estado, métricas y grabador)
  diagnostics.py  Estado de conexión y causas probables cuando no llegan datos
  replay.py       Reemisión de una captura por UDP respetando los tiempos
  playback.py     Replay interno: play/pause, velocidad, bucle y salto a vuelta
  fingerprint.py  Huella determinista, RunInfo y metadatos reproducibles
  recorder.py     Grabador en vivo, ingesta offline y serialización de la configuración
  weekend_profile.py   Perfil del fin de semana: agregación por compuesto, JSON canónico y huella
  weekend_provider.py  Localiza el fin de semana en SQLite y construye su perfil para el motor
  summary.py      Resumen de una captura (comando inspect)
  console.py      Panel de consola con Rich (solo presentación)
  console_messages.py  Renderizado en español de los diagnósticos
  cli.py          Comandos run, play, replay, inspect, trim, metrics, verify, datasets, ingest, sessions,
                  weekends y weekend
  strategy/       Tipos del contrato, modelo de ritmo y desgaste, pérdida en boxes y reglas de neumáticos
  metrics/        Tipos, configuración, histórico, resúmenes de tanda, módulos, motor y exportación
  store/          SQLite versionado, consultas de sesiones y exportación fiel a metrics
tests/            Pruebas unitarias, de integración y de extremo a extremo
docs/             Requerimientos, outline, roadmap y documentación de almacenamiento/exportación
config/config.example.toml   Ejemplo de configuración
```

Comandos útiles:

```bash
uv sync                              # instalar dependencias
uv run pytest                        # pruebas
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run f1-strategist run --listen-address 0.0.0.0   # recibir y grabar
uv run f1-strategist inspect captures/<archivo>.f1cap
uv run f1-strategist play captures/<archivo>.f1cap --speed 4
uv run f1-strategist replay captures/<archivo>.f1cap --host 127.0.0.1 --port 20777
uv run f1-strategist metrics captures/<archivo>.f1cap --format json
uv run f1-strategist verify captures/<archivo>.f1cap
uv run f1-strategist ingest captures/<archivo>.f1cap --db captures/sessions.db
uv run f1-strategist sessions list --db captures/sessions.db
uv run f1-strategist weekends list --db captures/sessions.db
uv run f1-strategist weekends link 3 4 --db captures/sessions.db
uv run f1-strategist weekend show --session 4 --db captures/sessions.db
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

## 10. Siguiente paso: Fase 4 y calibración diferida

La Fase 3 está implementada y documentada. El criterio de salida se verificó localmente en Windows; queda
repetir CI en Linux y macOS. La Fase 2 mantiene abierta la semántica del indicador de combustible del MFD
y el fixture de Monza acredita solo los escenarios observados localmente. El siguiente trabajo es:

1.  **Fase 4:** S1--S9 y S12--S13 implementadas (panel, registro SQLite v7 y prior por circuito).
    Siguiente: S10 (validación E2E/rendimiento) y S11 (documentación integral).
2.  **CI:** volver a ejecutar GitHub Actions después de la Ola 2.
3.  **Calibración diferida:** ajustar los parámetros que hoy no tienen valor universal y ampliar la
    validación a capturas con lluvia, safety car y otras condiciones.

## 11. Pendientes y decisiones abiertas

-   Captura más variada: safety car y, si es posible, lluvia.
-   Probar la topología de red local con dos PC; decidir envío dirigido o broadcast y política de
    filtrado por IP de origen.
-   Modelo local concreto del LLM; voz; framework del front-end de la GUI; empaquetado de escritorio;
    fórmulas de degradación y simulación de paradas; umbrales iniciales; idioma por defecto y cambio en
    caliente.
-   Ejecutar CI de GitHub Actions después de la Ola 5; la calibración de parámetros abiertos y las
    condiciones de lluvia o safety car quedan para fases posteriores con capturas variadas.
