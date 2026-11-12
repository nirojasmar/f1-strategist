# Almacenamiento SQLite de sesiones

Las fases 3 y 3.5 guardan cada sesión en dos capas complementarias:

1. La **captura cruda** (`.f1cap` o `.f1cap.gz`) conserva los datagramas tal como llegaron,
   con su tiempo de recepción, la IP y el puerto de origen. Es la fuente para el replay y
   para regenerar la base.
2. La **base SQLite** guarda datos derivados y consultables: metadatos de la sesión,
   participantes, vueltas, muestras de métricas, eventos y errores de la aplicación.

La captura no es una copia de seguridad completa de la base. Regenerarla con `ingest` crea
una sesión con la fecha de la ingesta, no con la original, y no restaura los errores de la
aplicación registrados durante una ejecución en vivo. Borrar la base sin copia pierde esos
registros de forma permanente; conviene conservar copias de `sessions.db` junto a las
capturas.

El esquema vive en [`src/f1_strategist/store/schema.py`](../src/f1_strategist/store/schema.py)
y la versión actual es `SCHEMA_VERSION = 7`. La versión se guarda en `PRAGMA user_version`.
Al abrir la base en modo escritura se aplican las migraciones pendientes. Los comandos
de consulta la abren en solo lectura y admiten v6 y v7 sin migrarlas; una base v6 no contiene
proyecciones por vuelta y sus evaluaciones no se pueden incluir en `strategy review`. Al abrir
una base compatible en modo escritura se aplican las migraciones hasta v7. La apertura de solo
lectura rechaza bases anteriores a v6 o creadas por una versión más nueva de la herramienta.

## Tablas

### `sessions`

Una fila por sesión grabada. `source` distingue una sesión en vivo (`live`) de una
reconstruida desde una captura (`ingest`).

| Columna | Contenido |
| --- | --- |
| `id` | Clave primaria. |
| `session_uid` | UID de la sesión del juego, como texto para conservar los 64 bits. |
| `source` | `live` o `ingest`. |
| `started_at_utc`, `ended_at_utc` | Inicio y cierre en UTC. |
| `incomplete` | `1` si se perdieron datos, la captura estaba truncada o falló la escritura. |
| `game_format`, `game_year`, `game_version` | Versión del juego observada. |
| `track`, `session_type`, `total_laps` | Metadatos de la sesión. |
| `capture_path`, `capture_sha256` | Captura asociada y su SHA-256. |
| `tool_version`, `parser_version` | Versiones de la herramienta y del parser. |
| `config_hash`, `config_json` | Configuración efectiva y su hash canónico. |
| `fingerprint` | Huella determinista de los datos derivados. |
| `final_metrics_json` | Instantánea final de métricas serializada (esquema v2). |
| `season_link_id`, `weekend_link_id`, `session_link_id` | Identificadores de enlace del juego; los ceros se normalizan a `NULL`. |
| `weekend_structure_json` | Orden de tipos de sesión anunciado por el juego. |
| `weekend_id` | Fin de semana al que pertenece la sesión, automático o manual. |
| `profile_json`, `profile_hash` | Perfil canónico usado al iniciar una carrera y su SHA-256. |
| `strategy_fingerprint` | SHA-256 de un objeto con la versión de esquema de estrategia y su configuración canónica. |

### `weekends`

Una fila por fin de semana enlazado. `source` distingue el enlace confirmado por
identificadores del juego (`game_ids`) de la agrupación manual (`manual`). No se crea
un fin de semana por guardar una sesión cuyos identificadores sean cero. Las sesiones
pueden agruparse manualmente solo si pertenecen al mismo circuito.

### `participants`

Participantes conocidos de la sesión. La clave es `(session_id, car_index)`. Los campos
`name` y `team` pueden ser `NULL` si el paquete de participantes aún no había llegado.

### `laps`

Una fila por coche y vuelta, con clave `(session_id, car_index, lap_number)`. Contiene todos
los campos de `LapRecord`: tiempos y sectores, validez, compuesto, edad y desgaste de
neumáticos, combustible, posiciones, intervalos, ERS, boxes, banderas, clima, tráfico,
daños y los relojes de inicio y fin. Las tuplas y conjuntos se guardan como JSON; los
booleanos como `0`/`1`.

### `metric_samples`

Muestras de métricas. Cada fila conserva el valor numérico o textual, la unidad, la calidad,
el número de muestras usadas, la vuelta y dos relojes distintos:

- `t_sim_s`: tiempo monotónico de captura (`observed_at`), siempre presente.
- `t_session_s`: tiempo de sesión de la cabecera del juego; puede ser `NULL` si la cabecera
  falta o no es válida.

La semántica de muestreo es importante para interpretar la tabla:

- Se guarda una muestra cuando cambia el valor o la calidad de una métrica.
- También se guarda una muestra al cerrar cada vuelta del piloto.
- Al cerrar la sesión se fuerza una muestra final para no perder el último estado.

Para reconstruir el valor mantenido de una vuelta se arrastra la última muestra conocida,
desde la primera vuelta con datos hasta la última vuelta realmente observada. No se
extrapola hasta `total_laps`. Una muestra con `lap IS NULL` anterior a la primera vuelta de
esa clave se usa como estado inicial; las muestras posteriores sin vuelta no se asignan a
ninguna vuelta.

El helper `held_metric_values(store, session_id, key_pattern="fuel.*", lap=None)` de
[`store/queries.py`](../src/f1_strategist/store/queries.py) implementa esa reconstrucción.

### `events`

Eventos de carrera en orden de inserción. `seq` es la secuencia asignada por el grabador;
`kind` es un código estable; `value` y `previous_value` conservan el cambio observado. Los
eventos idénticos consecutivos se conservan: la deduplicación se hace por alineación
ordenada de la ventana de eventos, no por un conjunto de campos.

### `app_errors`

Errores y avisos de la aplicación asociados a la sesión. `code` es una clave neutra al
idioma y `params_json` guarda los parámetros del diagnóstico.

### `stint_summaries`

Resumen por sesión, coche y tanda. Guarda compuesto real y visual, rango de vueltas,
vueltas limpias, edades y combustible al inicio/final, ritmo bruto y corregido,
tendencias, desgaste, temperatura, clima, representatividad y calidad. La clave
`(session_id, car_index, stint_index)` hace idempotente la persistencia. Los resúmenes
se generan al cerrar la sesión a partir del histórico de vueltas; si una sesión se
interrumpe, se guardan los datos disponibles y la sesión queda marcada como incompleta.

### `tyre_inventory_snapshots`

Instantáneas de cada juego de neumáticos del piloto. `kind` es `session_end` o
`race_start`; la clave `(session_id, kind, set_index)` evita duplicados. El perfil
prefiere el inventario de inicio de carrera y, si falta, usa la última instantánea de
cierre de sesión.

### `strategy_evaluations`

Evaluaciones publicadas por el motor de estrategia, en orden por sesión y secuencia. Se
guarda una evaluación por vuelta y otra cada vez que cambia la revisión de la recomendación.
La fila conserva la acción y alternativa recomendadas, la ganancia estimada y su intervalo,
la confianza, calidad de datos, huellas de entradas y perfil, y el JSON completo de la
evaluación. Desde v7 también conserva el índice del coche, compuesto y tiempos de vuelta
proyectados desde la siguiente vuelta completa; estas columnas permiten comparar cada
horizonte con vueltas reales válidas posteriores sin reconstruir el modelo. Las filas v6
conservan `NULL` en esos campos. La clave `(session_id, seq)` permite reescribir una secuencia
sin duplicar filas.

### Perfil del fin de semana

`profile_json` conserva el documento versionado (`schema_version`) exacto que alimentó
las métricas `profile.*` de la carrera; `profile_hash` es SHA-256 de ese JSON canónico.
El perfil agrupa los resúmenes de tandas por compuesto real y conserva procedencia,
muestras, calidad e inventario. La lectura del perfil no bloquea la captura: si no hay
un fin de semana enlazado no se registra uno, y los errores de lectura se notifican.

Al ingerir de nuevo una captura cuyo SHA-256 ya tiene un perfil registrado, `ingest`
valida la versión y el hash del JSON y aplica ese mismo perfil a las métricas de las
sesiones de carrera; no lo recalcula desde datos que pudieron cambiar. Las sesiones
de práctica y clasificación no reciben esas métricas.

### Índices

- `idx_sessions_started_at`: ordenar sesiones por fecha.
- `idx_sessions_capture_sha256`: buscar una captura ya ingerida.
- `idx_metric_samples_lookup`: recorrer muestras por sesión y clave.
- `idx_events_lookup`: recorrer eventos por sesión y secuencia.
- `idx_app_errors_session`: errores por sesión.
- `idx_sessions_weekend_id`: localizar las sesiones de un fin de semana.
- `idx_weekends_game_link`: unicidad de identificadores de temporada/fin de semana.
- La clave primaria de `strategy_evaluations` permite recorrer sus filas por sesión y secuencia.

## Consultas de ejemplo

Listar sesiones:

```sql
SELECT id, source, started_at_utc, track, session_type, total_laps, incomplete
FROM sessions
ORDER BY id;
```

Vueltas de un coche:

```sql
SELECT lap_number, lap_time_s, valid, compound, pit_entry, pit_exit
FROM laps
WHERE session_id = 1 AND car_index = 0
ORDER BY lap_number;
```

Eventos de una sesión:

```sql
SELECT seq, kind, t_sim_s, car_index, lap_number, value, previous_value
FROM events
WHERE session_id = 1
ORDER BY seq;
```

Última muestra conocida de una métrica:

```sql
SELECT lap, t_sim_s, t_session_s, value_num, value_text, unit, quality
FROM metric_samples
WHERE session_id = 1 AND key = 'fuel.margin_laps_at_finish'
ORDER BY id DESC
LIMIT 1;
```

Sesiones incompletas:

```sql
SELECT id, started_at_utc, track, source
FROM sessions
WHERE incomplete = 1
ORDER BY id;
```

Las mismas consultas están expuestas por la CLI, que abre la base en solo lectura:

```bash
uv run f1-strategist sessions list --db captures/sessions.db
uv run f1-strategist sessions show 1 --db captures/sessions.db
uv run f1-strategist sessions laps 1 --car 0 --db captures/sessions.db
uv run f1-strategist sessions events 1 --kind safety_car_changed --db captures/sessions.db
uv run f1-strategist sessions metrics 1 --key 'fuel.*' --db captures/sessions.db
uv run f1-strategist sessions export 1 --format json --db captures/sessions.db
uv run f1-strategist weekends list --db captures/sessions.db
uv run f1-strategist weekends show 1 --db captures/sessions.db
uv run f1-strategist weekend show --session 1 --db captures/sessions.db
```

`held_metric_values` no se limita a leer la última fila: rellena las vueltas observadas
arrastrando el valor anterior, que es lo que muestra `sessions metrics`. `sessions export`
con `--format json` reproduce exactamente la instantánea final guardada en
`final_metrics_json`. Las sesiones migradas desde el esquema v1 pueden tener ese campo vacío:
en ese caso la instantánea se reconstruye con valores neutros (ventana instantánea y calidad
de la última muestra) y puede no coincidir exactamente con la salida de `metrics`. Con
`jsonl` y `csv` se reproducen las vueltas y coinciden con `metrics`.

## Regenerar una base desde una captura

```bash
uv run f1-strategist ingest captures/session-YYYYMMDD-HHMMSS.f1cap \
  --config config/config.toml \
  --db captures/sessions.db
```

Usa `--config` con el mismo archivo TOML que estaba activo al grabar la captura. La
configuración afecta a las métricas personalizadas y al hash canónico, así que conviene
conservarla junto a la captura para regenerar la base con los mismos parámetros.

`ingest` reproduce la captura de forma offline con el mismo motor de métricas y el mismo
grabador que la sesión en vivo. La operación es idempotente: si ya existe una sesión
completa con la misma captura y la misma configuración, no se duplica. Si la sesión
anterior está incompleta, sin huella, sin instantánea final o hecha con otro parser o
versión, se vuelve a ingerir.

Para reemplazar una sesión existente:

```bash
uv run f1-strategist ingest CAPTURA --db captures/sessions.db --replace
```

El reemplazo procesa primero la captura y borra las sesiones antiguas solo si la nueva
ingesta termina correctamente. Si la captura está truncada, la sesión nueva queda marcada
como incompleta y la CLI lo avisa.

La huella de `ingest` usa la misma construcción canónica que `verify`: incluye todos los
eventos, las vueltas, la instantánea final de métricas, la configuración efectiva, la
versión del parser y el SHA-256 de la captura. Por eso ambas órdenes deben dar la misma
huella para la misma captura y configuración.

## Añadir una migración

Las migraciones son inmutables y se aplican en orden. Para cambiar el esquema:

1. Añade una tupla de sentencias nuevas en `schema.py`, con la siguiente versión disponible.
2. Incrementa `SCHEMA_VERSION` y añade la migración correspondiente al final de `MIGRATIONS`.
   No edites migraciones ya publicadas.
3. Actualiza `_SESSION_COLUMNS`, `SessionInfo`, `create_session`, `update_session`,
   `finish_session`, `_session_from_row` y los métodos de lectura o escritura afectados.
4. Añade una prueba que abra una base de la versión anterior, aplique la migración y
   compruebe los datos y la versión resultante.

`apply_migrations` valida que las versiones sean contiguas, ejecuta cada migración en una
transacción `BEGIN IMMEDIATE`, actualiza `PRAGMA user_version` en la misma transacción y
revierte DDL y versión si algo falla. Una base con una versión futura se rechaza sin
modificarla.

## Relación con la captura y la integridad

- La captura es la fuente de verdad para volver a calcular vueltas, eventos y métricas.
- `capture_sha256` identifica el archivo; `config_hash` identifica la configuración.
- `fingerprint` identifica el resultado derivado y permite comparar ejecuciones.
- `strategy_fingerprint` es la huella SHA-256 de un objeto con `schema_version` y la configuración
  de estrategia serializada de forma canónica; no depende del sistema operativo ni del orden de las
  claves.
- Guardar o cambiar `strategy_fingerprint` no modifica el valor de `fingerprint` ya almacenado.
  En cambio, `compute_capture_fingerprint` recalcula la huella a partir de la configuración completa,
  incluida la estrategia; al recalcularla tras un cambio de estrategia, el nuevo resultado puede diferir
  del valor almacenado. Compara los valores almacenados entre sí, o los resultados recalculados bajo la
  misma configuración, no un valor almacenado con otro recalculado bajo una configuración distinta.
- `incomplete = 1` indica que faltan datos: truncamiento de captura, pérdida de lotes en la
  cola del grabador, error de escritura o cierre anómalo. La sesión sigue siendo consultable,
  pero no debe usarse como referencia completa.
