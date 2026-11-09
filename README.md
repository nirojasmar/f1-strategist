# F1 Strategist

Ingeniero de carrera con IA para **F1 25 (2026 Season Pack)**: recibe la telemetría UDP del juego,
calcula estrategia y se comunica con el piloto.

**Estado:** Fases 0 a 3 implementadas y validadas localmente en Windows. La aplicación
recibe y graba telemetría UDP, decodifica los nueve tipos de paquete F1 25, mantiene snapshots
inmutables de la sesión y los coches, calcula métricas derivadas y permite reproducir sesiones
de forma determinista. La grabación SQLite está desactivada por defecto y se activa con
`recording.enabled = true` en el TOML. Incluye captura `.f1cap`, replay interno y por UDP,
ingesta offline, exportación de métricas y consultas de sesiones. La estrategia y el LLM
llegan en fases posteriores. Queda repetir CI en Linux y macOS después de la Ola 2.

La documentación de diseño está en [`docs/`](docs/): requerimientos, outline técnico y roadmap.

## Requisitos

- Windows o Linux (Ubuntu/Mint) en el PC donde se ejecuta la aplicación.
- [`uv`](https://docs.astral.sh/uv/) (instala Python 3.12+ y las dependencias automáticamente).

## Instalación

```bash
git clone https://github.com/nirojasmar/f1-strategist.git
cd f1-strategist
uv sync
```

## Uso

```bash
# Recibir telemetría (mismo PC que el juego), grabar y mostrar el estado
uv run f1-strategist run

# Recibir desde otro PC de la red local (escucha en todas las interfaces)
uv run f1-strategist run --listen-address 0.0.0.0 --allow-source 192.168.1.20

# Resumir una captura
uv run f1-strategist inspect captures/session-YYYYMMDD-HHMMSS.f1cap

# Crear una captura pequeña comprimida con paquetes seleccionados, rango de vueltas y datos anonimizados
uv run f1-strategist trim captures/session-YYYYMMDD-HHMMSS.f1cap tests/fixtures/real.f1cap.gz --types 2,4,7,10,11 --decimate 7=2 --decimate 11=2 --laps 3-5 --anonymize

# Reenviar una captura por UDP, sin abrir el juego
uv run f1-strategist replay captures/session-YYYYMMDD-HHMMSS.f1cap --host 127.0.0.1 --port 20777

# Reproducir internamente, con pausa, velocidad y salto de vuelta
uv run f1-strategist play captures/session-YYYYMMDD-HHMMSS.f1cap --speed 4

# Calcular y exportar métricas sin red
uv run f1-strategist metrics captures/session-YYYYMMDD-HHMMSS.f1cap --format json

# Comprobar la huella determinista de una captura
uv run f1-strategist verify captures/session-YYYYMMDD-HHMMSS.f1cap

# Guardar la sesión en SQLite y consultarla después
uv run f1-strategist ingest captures/session-YYYYMMDD-HHMMSS.f1cap --db captures/sessions.db
uv run f1-strategist sessions list --db captures/sessions.db
uv run f1-strategist sessions export ID --format json --db captures/sessions.db

# Agrupar sesiones del mismo circuito en un fin de semana y consultarlo
uv run f1-strategist weekends list --db captures/sessions.db
uv run f1-strategist weekends link 1 2 3 --label "Monza" --db captures/sessions.db
uv run f1-strategist weekends show 1 --db captures/sessions.db
uv run f1-strategist weekends unlink 3 --db captures/sessions.db
```

`ingest` informa del ID de la sesión creada; usa ese valor (o el que muestra `sessions list`)
en los comandos `sessions` y `weekends`.

### Fines de semana

Las sesiones se agrupan en fines de semana de dos formas:

- **Identificadores del juego.** Si el paquete `Session` trae `season_link_identifier`,
  `weekend_link_identifier` y `session_link_identifier` con valor distinto de cero, la sesión se
  enlaza automáticamente en un fin de semana con `source = game_ids`. Los identificadores a cero se
  guardan como `NULL` y no crean fin de semana: la sesión queda «sin enlace».
- **Enlace manual.** `weekends link` agrupa sesiones ya grabadas. Exige que todas sean del mismo
  circuito (rechaza el enlace con un error si no lo son) y es idempotente: volver a enlazar las
  mismas sesiones reutiliza el fin de semana existente y absorbe los que queden vacíos. Con
  `--label` se fija o cambia el nombre; si se omite, se usa el circuito.

| Comando | Qué hace |
| --- | --- |
| `weekends list` | Lista los fines de semana con su circuito, origen, número de sesiones y enlace del juego. Añade al final las sesiones que aún no tienen fin de semana. |
| `weekends show ID` | Muestra los metadatos de un fin de semana y la lista de sus sesiones. |
| `weekends link ID [ID ...]` | Enlaza manualmente las sesiones indicadas (`--label` opcional). |
| `weekends unlink ID` | Separa una sesión de su fin de semana; si el fin de semana queda vacío, se elimina. |

`sessions show ID` incluye el fin de semana asignado, el enlace del juego y la estructura del fin
de semana cuando el juego los publica.

El esquema de la base, la semántica de las muestras de métricas y cómo regenerar una base desde
una captura están documentados en [`docs/almacenamiento_sqlite.md`](docs/almacenamiento_sqlite.md).

El lector de capturas acepta `.f1cap.gz` (por ejemplo, con `inspect`, `replay` y `trim`), y `trim`
puede escribir `.f1cap` o `.f1cap.gz`; la salida comprimida es determinista. El rango `--laps A-B`
conserva los paquetes desde la primera muestra de la vuelta A hasta antes de la primera muestra de la
vuelta B+1, e incluye el último paquete `Session` y `Participants` anteriores a la ventana. Por eso,
la captura puede empezar hasta unos 5 segundos antes de la vuelta A.

`--decimate ID=N` conserva un paquete de cada N para ese ID. Para los paquetes rotatorios 11
(`SessionHistory`) y 12 (`TyreSets`), la cuenta se aplica independientemente por coche.
`--anonymize` descarta `LobbyInfo`, reemplaza nombres de pilotos (salvo nombres de IA con
`--keep-ai-names`), anonimiza los UID de sesión, elimina identificadores de enlace, metadatos de
plataforma, tiempos absolutos y direcciones de origen. Se conservan las diferencias entre tiempos de
recepción y los nombres públicos de IA solo si se solicita explícitamente.

Las opciones también pueden ir en un archivo TOML (ver
[`config/config.example.toml`](config/config.example.toml)):
`uv run f1-strategist run --config config.toml`.

## Configurar el juego

En el menú de ajustes de telemetría de F1 25 (los nombres exactos pueden variar según el idioma):

| Ajuste | Valor |
| --- | --- |
| Telemetría UDP | Activada |
| Dirección IP | La del PC que ejecuta la aplicación (`127.0.0.1` si es el mismo PC) |
| Puerto | `20777` (el mismo que `listen_port`) |
| Frecuencia de envío | `20 Hz` |
| Formato UDP | `2025`, paquete v1 |

Si otra aplicación de telemetría ya usa el puerto 20777, la aplicación avisa de que no puede abrirlo.

## Dos topologías

- **Mismo PC:** juego y aplicación en el mismo equipo. `listen_address = "127.0.0.1"` (valor por defecto).
- **Red local:** el juego en un PC y la aplicación en otro. En la aplicación, `listen_address = "0.0.0.0"`
  (o la IP de la interfaz LAN) y, opcionalmente, `allowed_sources` con la IP del PC del juego. En el juego,
  la IP de destino es la del PC de la aplicación. Se recomienda conexión cableada.

### Firewall del PC de la aplicación (solo topología de red local)

Windows (PowerShell como administrador):

```powershell
New-NetFirewallRule -DisplayName "F1 Strategist UDP 20777" -Direction Inbound -Protocol UDP -LocalPort 20777 -Action Allow
```

Ubuntu/Mint (si el firewall `ufw` está activo):

```bash
sudo ufw allow from 192.168.1.20 to any port 20777 proto udp   # IP del PC del juego
```

## Formato de captura (`.f1cap`, v1)

Datagramas en bruto, tal como llegaron, con la hora de recepción, la IP y el puerto de origen. La
estructura está documentada en [`src/f1_strategist/capture.py`](src/f1_strategist/capture.py). Las capturas
se guardan en `captures/` (ignorado por git). El lector también acepta archivos gzip `.f1cap.gz`.

## Desarrollo

```bash
uv run pytest            # pruebas
uv run ruff check .      # lint
uv run ruff format .     # formato
uv run pyright           # tipos
```

La integración continua ejecuta las pruebas en Windows y Linux.

El parser valida los layouts con fixtures compactos y anonimizados en
[`tests/fixtures/`](tests/fixtures/); las capturas completas permanecen fuera del repositorio.
