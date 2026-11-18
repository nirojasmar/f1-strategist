# AGENTS.md

Guía para agentes de IA que trabajan en `f1-strategist`: ingeniero de carrera local para F1 25 que recibe telemetría UDP, calcula métricas y estrategia con código determinista y usa un LLM solo para comunicar.

## Cómo orientarte (lee solo lo necesario)

| Necesitas | Lee |
| --- | --- |
| Estado y decisiones del proyecto | `progress.md` |
| Qué construir y por qué | `docs/requerimientos_ingeniero_carrera_f1_25.md` |
| Arquitectura y stack | `docs/outline_tecnico_ingeniero_carrera_f1_25.md` |
| Orden de fases | `docs/roadmap_ingeniero_carrera_f1_25.md` |
| Tu tarea concreta | El plan de fase indicado en la tarea (`plans/plan_fase_*.md`) |
| Formato de paquetes UDP | Especificación oficial de F1 25 (la del repo manda sobre librerías externas) |

No releas documentos completos si la tarea indica qué secciones leer. Usa `grep` o rangos de líneas en archivos grandes.

## Comandos

```bash
uv sync                         # instalar dependencias
uv run ruff check .             # lint
uv run ruff format --check .    # formato (usa `ruff format .` para aplicar)
uv run pyright                  # tipos
uv run pytest                   # pruebas
```

Una tarea está terminada solo si los cuatro comandos pasan.

## Stack

Python 3.12+, `uv`, `pyproject.toml`, `asyncio` con colas acotadas, `pydantic` v2, configuración TOML, `pytest` + `pytest-asyncio` + `hypothesis`. Solo biblioteca estándar para cálculo salvo decisión documentada. Debe funcionar en Windows y Linux: rutas con `pathlib`, sin dependencias específicas del sistema operativo.

## Estructura

```text
src/f1_strategist/   código (parser, race_state, session, metrics/, config, console, cli...)
tests/               pruebas; scenarios.py construye carreras sintéticas
tests/fixtures/      datagramas y capturas pequeñas anonimizadas
config/              config.example.toml
docs/                requisitos, outline, roadmap, planes de fase, revisiones
```

## Principios de diseño (no negociables)

1. **Código determinista decide y calcula; el LLM solo explica y conversa.** El LLM nunca modifica cifras.
2. **Silencio por defecto:** recibir telemetría no implica generar un mensaje.
3. **Núcleo independiente de la interfaz:** la consola es un adaptador sin lógica estratégica.
4. **Un solo escritor** del estado; los consumidores reciben snapshots inmutables.
5. **El receptor UDP y el registro nunca esperan al LLM** ni a ninguna interfaz.
6. **Datos desconocidos son `None`**, nunca 0 ni valores inventados. Distingue `unknown`, `stale` y válido. Con telemetría restringida (ceros del juego) publica `None`.
7. **Trazabilidad:** toda recomendación debe poder explicarse con datos, supuestos y reglas.
8. **Seguridad por defecto:** escuchar solo en `127.0.0.1`; abrir a la red es explícito.

## Convenciones de código

- `from __future__ import annotations` en todos los módulos.
- Dataclasses `frozen=True, slots=True` para modelos de dominio; no mutes snapshots.
- Líneas de 100 columnas; identificadores y comentarios técnicos en inglés.
- Cálculos puros: sin `time`, E/S ni hilos dentro de la lógica. El instante llega como parámetro.
- **Ningún texto visible al usuario en la lógica.** Usa claves de mensaje (`messages.message(...)`); la traducción vive en `console_messages.py`.
- Registros y eventos usan códigos neutros al idioma; el texto localizado es un dato adicional.
- Unidades explícitas en nombres y docs (`_s`, `_kg`, `_pct`, `_m`). Convierte milisegundos a segundos una sola vez.
- Errores de configuración: `ConfigError` con mensaje claro. Valida con pydantic (`extra="forbid"`).
- Aísla fallos de módulos opcionales (métricas, eventos): un error no debe detener la captura.

## Pruebas

- Toda funcionalidad nueva lleva pruebas; cada criterio de aceptación debe tener una.
- **No dependas de capturas reales completas** (no están versionadas). Usa `tests/scenarios.py` y `tests/fixtures/`.
- Cubre siempre: datos ausentes (`None`), umbrales límite, reinicio de sesión (`session_uid`), paquetes inválidos o truncados y flashback.
- Las pruebas de rendimiento o con captura real deben omitirse limpiamente si falta el recurso.
- Pruebas deterministas: sin red, sin `sleep`, sin reloj real.

## Flujo de trabajo con git

- Una tarea, un commit: `<id>---<slug>` (ej. `E1 --- Tipos, configuración y esqueleto`).
- Toca solo los archivos que la tarea posee. Excepción: una línea en `registry.py` o en `config.example.toml`.
- No cambies APIs públicas existentes sin acordarlo; si es necesario, detente y pregunta.
- Commits pequeños y descriptivos. No hagas merge ni force-push a `main`.
- `progress.md` solo se actualiza en la tarea de documentación de cada fase.

## Datos y privacidad

- Nunca versiones capturas completas (`*.f1cap`) ni bases SQLite locales.
- Fixtures versionados deben estar anonimizados (nombres de participantes e IDs de red).
- No incluyas secretos, tokens ni IPs personales en código, tests o docs.

## Cuándo detenerte y preguntar

- Falta una dependencia de otra tarea que no está en `main`.
- La tarea contradice el plan, los requisitos o estos principios.
- Necesitas cambiar una API pública o salir del alcance de la tarea.
- Un fallo de lint, tipos o pruebas es ajeno a tu cambio: repórtalo, no lo arregles.

## Al terminar

Informe breve: archivos tocados, criterios de aceptación (✅/❌), resultado de los comandos, suposiciones tomadas y bloqueos. No reimprimas código ni diffs completos.

## Agentes especializados

Si existen estos skills, úsalos en lugar de improvisar el proceso:

- `f1-implementar-tarea`: implementar una tarea de un plan de fase.
- `f1-wave-review`: revisar una ola y documentar hallazgos en `review_fase_X_ola_Y.md`.
