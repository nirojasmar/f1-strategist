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
| Receptor UDP | Hilo dedicado con socket bloqueante y cola acotada | Decisión cerrada; anomalías de secuencia medidas por tipo y `session_uid` |
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
-   La cabecera de 29 bytes y los IDs de tipo de paquete (0 a 15) coinciden con la especificación oficial
    de EA (formato 2025) y funcionan con la versión 1.26 del Season Pack.
-   Los tamaños de los 14 tipos de paquete presentes en la captura coinciden exactamente con los de la
    especificación.
-   El orden de campos de `LapData` de la especificación es consistente con los datos reales (sección 7).
-   `LapData.m_gridPosition` es de base 0 (parrilla menos 1), mientras que `FinalClassification.m_gridPosition`
    es de base 1 (sección 7).
-   La captura es una carrera de 5 vueltas con 22 coches.

**No verificado todavía:**

-   Recepción desde un segundo PC de la red local (topología B) y filtro de IP de origen con tráfico real.
-   Ejecución en Linux con el juego real.
-   Primera ejecución del CI en GitHub Actions (Windows y Ubuntu).
-   Si la captura de 5 vueltas incluye paradas en boxes (`m_numPitStops`).
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
        sobre datos reales.
-   **Base de `m_gridPosition`:** cruzando `LapData.m_gridPosition` con `m_carPosition` del primer
    `LapData` y con `m_gridPosition` de `FinalClassification` (offset 2 de cada coche; 46 bytes por coche
    tras la cabecera y `m_numCars`), el valor de `LapData` es siempre la parrilla menos 1 (base 0) para
    los 22 coches. `FinalClassification.m_gridPosition` y `m_carPosition` al inicio coinciden en base 1.
    La especificación no lo indica. El parser debe normalizar a base 1 y cubrirlo con una prueba.
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

## 10. Siguiente paso: plan de la Fase 1

1.  **Receptor, cerrado:** hilo dedicado con socket bloqueante y cola acotada; anomalías de secuencia
    medidas y visibles por tipo de paquete. La prueba en red local con dos PC sigue pendiente.
2.  **Parser:** interfaz y registro por tipo de paquete, layouts según la especificación de EA (formato 2025), validación de tamaños y rangos, y soporte
    únicamente de los formatos conocidos (aviso ante formatos no soportados). Orden: `Session`,
    `LapData`, `Participants`, `CarStatus`, `CarDamage`, `TyreSets`, `SessionHistory`, `Event`,
    `FinalClassification`.
3.  **Estado de carrera:** modelos de sesión, coche del piloto y rivales; frescura por grupo de datos;
    detección de cambio de sesión, de vuelta y de parada; panel de resumen de sesión en consola.
4.  **Validación:** pruebas con fixtures reales, pruebas de propiedades con paquetes corruptos y replay
    de la captura completa sin excepciones.

## 11. Pendientes y decisiones abiertas

-   Comprobar si la captura de 5 vueltas incluye paradas en boxes (`m_numPitStops`).
-   Captura más variada: carrera con paradas en boxes, safety car y, si es posible, lluvia.
-   Probar la topología de red local con dos PC; decidir envío dirigido o broadcast y política de
    filtrado por IP de origen.
-   Esquema de SQLite; modelo local concreto del LLM; voz; framework del front-end de la GUI;
    empaquetado de escritorio; fórmulas de degradación y simulación de paradas; umbrales iniciales;
    idioma por defecto y cambio en caliente.
-   Registrar en `docs/` los hallazgos confirmados (formato UDP 2025 válido, versión 1.26, broadcast).
