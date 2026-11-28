# Preguntas del piloto

El piloto responde preguntas de texto sobre la sesión activa. El cálculo y la
respuesta principal son deterministas; el LLM solo añade, si está habilitado, un
seguimiento explicativo. El flujo funciona sin LLM y no espera al cooldown ni al
silenciado de avisos espontáneos.

## Arquitectura

`PilotEngine` publica el acuse antes de consultar colaboradores. Después toma
snapshots inmutables, el `Intent Router` clasifica la pregunta y resuelve
referencias a pilotos, la herramienta correspondiente calcula hechos y el
compositor produce la respuesta localizada. El intercambio se mantiene en la
memoria de diálogo y se puede registrar en SQLite. El seguimiento del agente se
envía por separado y nunca sustituye ni retrasa la respuesta determinista.

El router normaliza mayúsculas, tildes y signos, puntúa frases y palabras clave
del catálogo TOML y aplica patrones negativos. Solo acepta una intención con
puntuación igual o mayor que `min_intent_confidence`; empates y preguntas fuera
de alcance quedan como `unknown`. El directorio resuelve nombres, equipos,
números, posiciones y referencias como «el de delante». Las referencias
ambiguas generan opciones ordenadas y una aclaración pendiente; las respuestas
de continuación pueden recuperar la intención reciente durante la vigencia del
diálogo.

## Intenciones y herramientas

| Intención | Función |
| --- | --- |
| `general_race_state` | Estado general de carrera |
| `ask_strategy` | Consulta o simulación de estrategia |
| `fuel_status` | Margen de combustible |
| `tyre_life` | Vida de neumáticos |
| `pace_diagnosis` | Diagnóstico de pérdida de ritmo |
| `compare_driver` | Comparación con otro piloto |
| `pit_undercut` | Evaluación de undercut |
| `gap_status` | Intervalos y brechas |
| `damage_status` | Daños del coche |
| `weather_forecast` | Pronóstico de clima |

El catálogo español está en `src/f1_strategist/pilot/intents/es.toml`. Las
herramientas se registran en `src/f1_strategist/pilot/__init__.py`; cada una
recibe `ToolInputs` y devuelve un `ToolResult`. Las preguntas que necesitan datos
que no están disponibles o actuales no inventan un valor: la respuesta declara
lo que falta o está obsoleto.

## Composición de la respuesta

Un `ToolResult` contiene hechos `ToolFact` con nombre, valor, unidad, calidad y
tipo:

- `measured`: medición publicada por el juego.
- `estimated`: estimación calculada por el motor.
- `recommended`: recomendación calculada por estrategia.

El compositor `pilot/answers.py` descarta del texto los valores `None`, muestra
los hechos con plantillas y vocabulario localizado, e identifica datos ausentes
y obsoletos. Las cifras de la respuesta determinista proceden de esos hechos,
nunca de texto generado por el LLM. Las claves y plantillas de respuesta están
en `console_messages.py`. Las aclaraciones y las preguntas desconocidas también
usan plantillas.

El acuse y la respuesta principal se publican sin esperar al Gate ni al LLM. El
seguimiento es opcional, solo se solicita para las intenciones declaradas en
`pilot.llm_followup_intents` y puede desactivarse con `pilot.llm_followup = false`
o `llm.enabled = false`.

## Parámetros

Los valores por defecto están en `config/config.example.toml`; son ajustables y
provisionales salvo donde se indica el límite. La configuración se valida con
`PilotConfig` (`extra = "forbid"`).

| Parámetro | Predeterminado | Efecto |
| --- | --- | --- |
| `pilot.enabled` | `true` | Habilita las preguntas del piloto. |
| `pilot.language` | `"es"` | Idioma del catálogo y de las plantillas. Solo hay catálogo español implementado actualmente. |
| `pilot.min_intent_confidence` | `0.6` | Puntuación mínima de intención, entre 0 y 1. |
| `pilot.dialogue_turns` | `3` | Turnos conservados para resolver continuaciones. |
| `pilot.dialogue_ttl_s` | `60` | Vigencia de la memoria de diálogo. |
| `pilot.clarification_ttl_s` | `30` | Vigencia de una aclaración pendiente. |
| `pilot.max_question_chars` | `200` | Longitud máxima de entrada de texto. |
| `pilot.ack_max_ms` | `50` | Límite objetivo para publicar el acuse. |
| `pilot.llm_followup` | `true` | Habilita el seguimiento explicativo opcional. |
| `pilot.llm_followup_intents` | estrategia, ritmo, undercut | Intenciones que pueden pedir seguimiento al LLM. |
| `pilot.followup_timeout_s` | `12` | Tiempo máximo del seguimiento. |
| `pilot.followup_max_words` | `40` | Longitud máxima del seguimiento. |
| `pilot.hedge_words` | lista en TOML | Expresiones de cautela exigidas al seguimiento que use estimaciones. |
| `pilot.pace_trend_stable_s` | `0.05` | Umbral de tendencia de ritmo considerada estable. |
| `pilot.undercut_horizon_laps` | `3` | Horizonte de la simulación de undercut. |
| `pilot.undercut_min_rival_clean_laps` | `3` | Vueltas limpias mínimas del rival. |
| `pilot.history_size` | `50` | Intercambios recientes conservados en el panel. |
| `pilot.record_text` | `true` | Si es `false`, el texto de la pregunta no se persiste en SQLite. |
| `gate.pilot_quiet_s` | `15` | Suprime avisos informativos tras una pregunta; no bloquea la respuesta. |

Los límites de la Fase 7 son: acuse antes de `ack_max_ms`; respuesta determinista
con media menor de 20 ms y p99 menor de 50 ms; exactitud de intención de al menos
95 % dentro de alcance, falsos positivos de hasta 5 % fuera de alcance y
aclaraciones correctas de al menos 95 %.

## Añadir una intención

1. Añadir el valor al enum `Intent` de `pilot/types.py`.
2. Añadir frases, palabras clave y patrones negativos al catálogo de cada idioma
   soportado en `pilot/intents/<idioma>.toml`.
3. Implementar la herramienta con salida `ToolResult`/`ToolFact` y registrarla
   en `pilot/__init__.py`.
4. Añadir plantillas y etiquetas para respuesta completa, parcial y sin datos en
   `console_messages.py`; incluirla en `SUGGESTED_INTENTS` solo si corresponde.
5. Añadir casos a los conjuntos de evaluación y pruebas de enrutado, herramienta,
   composición, datos ausentes y registro.

Las claves de intención se guardan como texto en SQLite; añadir una intención no
requiere una migración mientras no cambie el esquema de trazabilidad.

## Añadir un idioma

Crear `pilot/intents/<idioma>.toml` con la tabla de cada intención, frases y
palabras clave ponderadas en `(0, 1]`, y patrones negativos. Añadir también las
plantillas, etiquetas y palabras de tipo en `console_messages.py`, registrar el
catálogo localizado en `pilot/answers.py` y cubrir la normalización, los acentos,
las respuestas completas/parciales/sin datos y las aclaraciones con pruebas. El
valor `pilot.language` debe coincidir con el nombre del archivo del catálogo.
Actualmente el único idioma implementado para el piloto es español.

## Preguntas guiadas

Para una pregunta puntual sobre una captura, se puede indicar la vuelta:

```powershell
uv run f1-strategist ask captures\carrera.f1cap --lap 5 "¿Cuánto combustible queda?"
```

`ask-script` ejecuta un guion JSON Lines en modo de plantillas, sin depender del
LLM. Cada línea debe contener `text` y exactamente uno de `at_lap` o
`at_session_time_s`:

```json
{"at_lap": 5, "text": "¿Cuánto combustible queda?"}
{"at_session_time_s": 900.0, "text": "¿Cómo voy respecto al de delante?"}
```

```powershell
uv run f1-strategist ask-script captures\carrera.f1cap preguntas.jsonl --json
```

La salida contiene las respuestas, pasos no activados y una huella determinista;
la latencia de ejecución y las rutas locales no forman parte de esa huella. Para
registrar los intercambios en SQLite, se puede añadir `--db captures\sessions.db`.
Los conjuntos etiquetados del router, entidades y aclaraciones se ejecutan con
`uv run f1-strategist pilot eval`.

En una sesión en vivo, la entrada de texto se ofrece desde la consola `run`.
`queries show`, `queries summary` y `queries export` inspeccionan o exportan las
consultas guardadas; `pilot.record_text = false` evita persistir el texto de
pregunta, pero conserva la trazabilidad estructurada de la respuesta.
