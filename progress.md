# Progreso del proyecto --- F1 Strategist

**Última actualización:** 2026-11-01
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
| Receptor UDP | Hilo dedicado con socket bloqueante y cola acotada | Implementación de Sprint 1; la comparación con `asyncio` sigue abierta |
| Formato `.f1cap` v1 | Definido en `src/f1_strategist/capture.py` | Implementado en Sprint 1 |
| Parser | Propio (`struct`), detrás de una interfaz | Acordado; aún no implementado |

## 4. Estado por fase del roadmap

| Fase | Estado | Notas |
| --- | --- | --- |
| 0. Definición y entorno | Parcial | Repo, CI definido, config, receptor y guía de firewall listos. Falta validar la recepción desde otro PC de la red local y en Linux real. |
| 1. Receptor, parser y estado | En curso | Receptor y captura listos. Parser por tipo de paquete y estado de carrera pendientes. |
| 2. Métricas e histórico | No iniciada | |
| 3. Grabación y replay | Parcial | Captura y reemisión UDP listas (se adelantaron en Sprint 1). Falta el replay interno hacia el pipeline y el registro en SQLite. |
| 4--10 | No iniciadas | |

## 5. Qué está verificado y qué no

**Verificado:**

-   Las 29 pruebas, `ruff` y `pyright` pasan en Linux (contenedor de desarrollo).
-   Flujo de extremo a extremo en Linux con procesos reales: `replay` -> `run` -> captura -> `inspect`.
-   **Recepción de telemetría real en Windows 11** con el juego: captura de 68 541 paquetes en
    ~13 minutos, sin paquetes inválidos.
-   La cabecera de 29 bytes (formato de F1 24/25) es correcta para la versión 1.26 del Season Pack.

**No verificado todavía:**

-   Recepción desde un segundo PC de la red local (topología B) y filtro de IP de origen con tráfico real.
-   Ejecución en Linux con el juego real.
-   Primera ejecución del CI en GitHub Actions (Windows y Ubuntu).
-   Pruebas (`pytest`) en Windows.
-   Que el paquete `Event` (1 202 en 13 min) contenga lo que se espera; se aclarará al parsear los códigos.

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
-   La página de la especificación oficial de EA no se pudo leer con las herramientas de desarrollo; los
    layouts de paquetes deben tomarse de un documento que aporte el usuario o de las librerías de referencia.

### 2026-11-01

-   Primera ejecución en Windows 11: la captura salió con 0 paquetes. **Causa: el juego tenía Broadcast
    Mode activado**; con la app escuchando solo en `127.0.0.1` no llegaban datos. Solución: escuchar en
    `0.0.0.0` (`--listen-address 0.0.0.0`).
-   Con esa configuración se grabó la primera captura real (ver sección 7).
-   El usuario indicó que no se edite código sin petición directa.
-   Creado este archivo.

## 7. Hallazgos de la primera captura real

Captura: 68 541 paquetes, 795,5 s (~13 min), ~66 MB. Salida de `inspect`:

-   **Orígenes:** `192.168.68.59` (68 538 paquetes; es la IP de red local del propio PC del juego) y
    `127.0.0.1` (3 paquetes). Con Broadcast Mode el origen es la IP de red local, no loopback; si se usa
    `allowed_sources` en ese caso, debe llevar esa IP.
-   **Cabecera:** los 68 540 paquetes del juego llevan formato 2025, año 25, versión 1.26. El paquete
    restante (formato 30840, año 120) es la prueba manual `b'x' * 40`, no del juego.
-   **Tipos presentes:** 0 a 8, 10 a 13 y 15. Ausentes: 9 (`LobbyInfo`, solo online) y 14 (`TimeTrial`,
    solo contrarreloj). Hay un `FinalClassification`, así que la captura incluye el cierre de sesión.

| ID | Tipo | Paquetes | Frecuencia aprox. |
| --- | --- | --- | --- |
| 0 | Motion | 8 674 | 10,9 Hz |
| 1 | Session | 869 | 1,1 Hz |
| 2 | LapData | 8 674 | 10,9 Hz |
| 3 | Event | 1 202 | no periódico |
| 4 | Participants | 88 | 0,11 Hz |
| 5 | CarSetups | 869 | 1,1 Hz |
| 6 | CarTelemetry | 8 674 | 10,9 Hz |
| 7 | CarStatus | 8 674 | 10,9 Hz |
| 8 | FinalClassification | 1 | una vez |
| 10 | CarDamage | 4 338 | 5,5 Hz |
| 11 | SessionHistory | 8 694 | 10,9 Hz |
| 12 | TyreSets | 8 674 | 10,9 Hz |
| 13 | MotionEx | 8 674 | 10,9 Hz |
| 15 | LapPositions | 435 | 0,55 Hz |

-   **Tráfico:** ~85 KB/s (~0,7 Mbit/s). Irrelevante para una red local, incluso por Wi-Fi.
-   **Cadencia:** ~11 Hz efectivos para los paquetes por fotograma. Suficiente para estrategia; no hace
    falta subir la frecuencia de envío del juego.
-   Las frecuencias son observadas en esta captura, no extraídas de la especificación.

## 8. Mapa del código

```text
src/f1_strategist/
  config.py       Configuración TOML validada (pydantic) y overrides de la CLI
  packets.py      Cabecera común de 29 bytes y nombres de tipos de paquete
  capture.py      Formato .f1cap v1: CaptureWriter, CaptureReader, CapturedPacket
  receiver.py     UdpReceiver (hilo + cola acotada), Stats y StatsSnapshot
  session.py      CaptureSession: receptor -> cola -> consumidor (captura + estadísticas)
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

-   Las capturas (`captures/`) no se versionan en git. Para fixtures de pruebas habrá que recortar una
    captura real a un tamaño razonable.
-   En Windows, usar PowerShell o Windows Terminal para la consola. Git Bash puede no redibujar el
    panel en vivo (hipótesis no confirmada).
-   `run` instala manejadores para Ctrl+C y SIGTERM: los procesos lanzados en segundo plano desde shells
    no interactivos ignoran SIGINT, y sin el manejador no se cerraba la captura limpiamente.
-   Si el juego envía por broadcast, la app debe escuchar en `0.0.0.0`; en loopback no recibe nada.
-   Otra aplicación de telemetría usando el puerto 20777 provoca un error claro al abrir el socket.

## 10. Siguiente paso: plan de la Fase 1 (propuesta, pendiente de aprobación)

Orden de trabajo propuesto (el detalle se discute en la conversación y se actualizará aquí al aprobarse):

1.  **Receptor, cierre:** detección de pérdida, duplicados y desorden con los identificadores de la
    cabecera; prueba en red local con dos PC; decidir hilo frente a `asyncio`.
2.  **Parser:** interfaz y registro por tipo de paquete, validación de tamaños y rangos, y soporte
    únicamente de los formatos conocidos (aviso ante formatos no soportados). Orden: `Session`,
    `LapData`, `Participants`, `CarStatus`, `CarDamage`, `TyreSets`, `SessionHistory`, `Event`,
    `FinalClassification`.
3.  **Estado de carrera:** modelos de sesión, coche del piloto y rivales; frescura por grupo de datos;
    detección de cambio de sesión, de vuelta y de parada; panel de resumen de sesión en consola.
4.  **Validación:** pruebas con fixtures reales, pruebas de propiedades con paquetes corruptos y replay
    de la captura completa sin excepciones.

## 11. Pendientes y decisiones abiertas

-   Fuente de referencia para los layouts de cada paquete (documento oficial del usuario o librerías de
    referencia contrastadas con la captura).
-   Captura más variada: carrera con paradas en boxes, safety car y, si es posible, lluvia.
-   Probar la topología de red local con dos PC; decidir envío dirigido o broadcast y política de
    filtrado por IP de origen.
-   Receptor UDP: mantener el hilo dedicado o pasar a `asyncio`.
-   Esquema de SQLite; modelo local concreto del LLM; voz; framework del front-end de la GUI;
    empaquetado de escritorio; fórmulas de degradación y simulación de paradas; umbrales iniciales;
    idioma por defecto y cambio en caliente.
-   Registrar en `docs/` los hallazgos confirmados (formato UDP 2025 válido, versión 1.26, broadcast).
