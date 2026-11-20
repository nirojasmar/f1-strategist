# Agente LLM local

La integración del LLM transforma alertas ya decididas por el motor determinista en
mensajes breves para el piloto. El cálculo de métricas, estrategia y eventos continúa
fuera del modelo: el LLM solo redacta una respuesta a partir de un contexto publicado.

## Arquitectura

El flujo es:

1. El detector publica una señal y el Event Gate decide si emitir, actualizar, aplazar,
   cancelar o silenciar un `AgentAlert`.
2. Solo una alerta comunicable inicia el contexto del agente.
3. `ContextBuilder` copia datos ya publicados y construye un documento compacto:
   prioridad, hechos, incertidumbres, recomendación, estrategia, datos obsoletos y
   mensajes recientes.
4. `PromptRegistry` añade el prompt versionado del idioma y una respuesta estructurada
   compatible con JSON Schema.
5. El cliente envía la petición a un runtime compatible con OpenAI.
6. `ResponseValidator` comprueba esquema, hechos respaldados, prioridad, idioma,
   longitud y restricciones numéricas.
7. El comunicador publica el texto válido o degrada a plantilla. La generación se
   ejecuta fuera del hilo consumidor y nunca bloquea recepción, métricas o grabación.

La interfaz `LLMClient` permite reemplazar el cliente HTTP por dobles deterministas,
respuestas grabadas o pruebas. Los mensajes visibles se mantienen separados de la
lógica: las plantillas viven en el catálogo localizado y los registros usan códigos
neutrales al idioma.

## Política de prioridades

Las prioridades proceden del Event Gate; el agente no las recalcula.

- **Crítica:** por defecto nunca usa el LLM (`critical_uses_llm = false`) y sale con
  plantilla. El timeout crítico por defecto es de 1,5 s.
- **Estratégica:** puede esperar hasta `timeout_strategic_s` (10 s por defecto).
- **Informativa:** puede esperar hasta `timeout_informational_s` (10 s por defecto).

El comunicador descarta una generación si la alerta queda cerrada, sustituida o supera
`max_alert_age_s`. Si hay un fallo, timeout o JSON inválido, el comportamiento de
degradación predeterminado es emitir la plantilla; `drop` solo se permite para
informativos.

No hablar no implica desactivar la captura. Si no hay alerta comunicable, el modelo no
recibe una llamada. `gate.muted = true` también silencia al agente sin detener el
registro ni los demás motores.

## Configuración

La configuración vive en `[llm]` y `[agent]`. Los parámetros principales son:

| Parámetro | Significado |
| --- | --- |
| `llm.enabled` | Activa el modo de generación; por defecto está desactivado. |
| `llm.endpoint` | URL base compatible con OpenAI, por ejemplo `http://127.0.0.1:1234/v1`. |
| `llm.model` | Identificador obligatorio del modelo servido. |
| `llm.api_key` / `F1_LLM_API_KEY` | Credencial opcional; la variable de entorno evita guardarla en TOML. |
| `llm.temperature`, `top_p`, `seed` | Muestreo y reproducibilidad del modelo. |
| `llm.max_tokens` | Presupuesto máximo de salida; por defecto 200. |
| `llm.stream` | Activa streaming para medir primer token y permitir cancelación. |
| `llm.structured_output` | Nivel `auto`, `json_schema`, `json_object` o `none`. |
| `llm.extra_body` | Parámetros adicionales específicos del servidor. |
| `llm.connect_timeout_s` | Timeout de conexión. |
| `llm.max_in_flight` | Máximo de generaciones concurrentes. |
| `llm.health_interval_s` | Intervalo de comprobación periódica de salud. |
| `agent.language` | Idioma del contexto y del texto final (`es` por defecto). |
| `agent.max_words`, `agent.max_chars` | Límites del mensaje emitido. |
| `agent.timeout_*_s` | Presupuestos por prioridad. |
| `agent.fallback_on_failure` | `template` o `drop` para informativos. |
| `agent.max_alert_age_s` | Edad máxima antes de descartar una generación. |
| `agent.recent_messages` | Mensajes recientes incluidos como contexto. |
| `agent.context_token_budget` | Presupuesto compacto del contexto. |
| `agent.offline_mode` | `template`, `recorded` o `live`. |
| `agent.record_context` | Guarda contexto y respuesta de cada llamada en SQLite. |

Los modelos que usan razonamiento deben desactivarlo si el servidor lo permite. La
medición de Gemma 4 E2B IT Q4_K_S mostró que el razonamiento podía consumir el
presupuesto de salida y producir JSON truncado; desactivarlo fue la mejora principal
para obtener respuestas estructuradas.

## Validación

El validador verifica el contenido contra el contexto publicado:

- JSON y esquema `LLMReply`.
- Lenguaje declarado.
- Prioridad igual a la de la alerta.
- Número de palabras y caracteres.
- Frases imperativas permitidas.
- `used_facts`: solo claves presentes en los hechos del contexto.
- Cifras: no puede aparecer un número no respaldado por un hecho.
- Respaldos: referencias, unidades y comparaciones deben pertenecer al contexto.

La salida no altera métricas ni recomendaciones. Una respuesta válida puede usarse
como texto; si falla una regla, se registra el código y se aplica la degradación
configurada.

## Modos de ejecución

- **`template`:** no llama al endpoint y usa plantillas locales. Es el modo seguro
  predeterminado mientras `llm.enabled` sea falso.
- **`recorded`:** reproduce respuestas ya grabadas para pruebas deterministas y
  evaluaciones sin red. Requiere respuestas en el conjunto o exportación.
- **`live`:** llama al runtime configurado. Solo se selecciona con un cliente vivo y
  `llm.enabled = true`.

Los modos son deterministas respecto del reloj simulado: `template` no cambia con la
velocidad de reproducción, y `recorded` reutiliza los textos registrados.

## Configurar LM Studio

### LM Studio local

1. Arrancar LM Studio y cargar el modelo elegido.
2. Iniciar el servidor HTTP compatible con OpenAI, normalmente en
   `127.0.0.1:1234`.
3. Configurar `[llm]` con `enabled = true`, `endpoint = "http://127.0.0.1:1234/v1"`
   y el identificador exacto del modelo.
4. Exportar `F1_LLM_API_KEY` solo si el servidor requiere autenticación.
5. Ejecutar `f1-strategist llm check --config config.toml`.

La escucha predeterminada de la aplicación sigue limitada a `127.0.0.1`.

### LM Studio remoto

El servidor puede ejecutarse en otro PC de la red local. En ese equipo:

1. Configurar LM Studio para escuchar en una interfaz alcanzable desde la aplicación.
2. Permitir únicamente el puerto necesario en el firewall.
3. Usar el endpoint HTTP o HTTPS de esa máquina en `llm.endpoint`.

El endpoint no debe contener usuario ni contraseña. El secreto debe entrar por
`F1_LLM_API_KEY`; nunca se registra en SQLite ni se incluye en huellas de configuración.

## Diagnóstico y evaluación

```bash
f1-strategist llm check --config config.toml
f1-strategist llm bench --runs 10 --warmup 1 --note "modelo-juego"
f1-strategist llm eval --dataset tests/fixtures/llm_eval --out informe.json
f1-strategist llm eval --from-session 4 --db captures/sessions.db --out informe.json
f1-strategist agent export 4 --db captures/sessions.db --out export.json
```

- `llm check` verifica la disponibilidad y el esquema estructurado del endpoint.
- `llm bench` mide primer token, latencia total y latencia por corrida.
- `llm eval --dataset` evalúa casos sintéticos con cliente grabado o modelo real.
- `llm eval --from-session` convierte llamadas exportadas de una sesión en casos
  reproducibles.
- Si no se indica `--model`, la evaluación intenta usar respuestas grabadas. Con
  `--model` usa el modelo configurado o el servidor indicado.

El informe de evaluación incluye esquema válido, cifras consistentes, longitud,
prioridad, idioma, `used_facts`, respaldo, latencia y el texto y regla de cada fallo.
No fija umbrales ni compara modelos: mide el resultado para repetibilidad.

## Resultado medido en la integración

La validación local con Gemma 4 E2B IT Q4_K_S y razonamiento desactivado obtuvo
24 de 34 casos válidos (70,59 %), con 29,41 % de fallback y esquema válido en 70,59 %.
Cifras, prioridad, idioma y longitud alcanzaron 100 % de validez; `used_facts`
97,06 %. La latencia global fue p50 7,0657 s y p95 9,2671 s.

Los críticos dependen de la plantilla por diseño: el timeout de 1,5 s impide esperar al
modelo. Esta medición no declara al modelo listo para producción ni sustituye la
validación adversarial y los benchmarks con el juego activo.
