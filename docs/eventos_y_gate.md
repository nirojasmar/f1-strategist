# Eventos y compuerta de eventos

El detector de eventos de la Fase 5 decide **cuándo** merece la pena avisar al piloto y la
compuerta decide **si ese aviso se emite ahora**. Ambas piezas son código determinista: no
llaman al LLM, no dependen del reloj real y no tienen estado oculto fuera de su instancia. El
silencio es el comportamiento por defecto; un aviso solo aparece cuando una condición se
mantiene y supera los controles de la compuerta.

## Arquitectura

```text
RaceStateSnapshot ─┐
MetricsSnapshot  ──┼─> EventRule ─> Observation ─> ConditionTracker ─> EventSignal ─> EventGate
StrategySnapshot ──┘                                      │                            │
                                                          └─ ciclo de vida              └─> GateDecision ─> AgentAlert
```

| Etapa | Responsabilidad |
| --- | --- |
| `EventRule` | Lee snapshots publicados y devuelve una `Observation`. No calcula estrategia ni métricas. |
| `ConditionTracker` | Aplica persistencia, histéresis y cambio material; convierte observaciones en señales `opened`/`updated`/`closed`. |
| `EventGate` | Ordena, limita y explica las señales. Devuelve una `GateDecision` por señal más las decisiones de las colas internas. |
| `AgentAlert` | Aviso inmutable y listo para consola, SQLite o el LLM de la Fase 6. |

Reglas de diseño que sostienen el resto del documento:

- Las reglas son funciones puras de los snapshots. No vuelven a evaluar estrategia ni
  reconstruyen datos: consumen lo ya publicado.
- El detector y la compuerta corren en el hilo del consumidor de sesión, nunca en el receptor
  UDP ni en el hilo de la interfaz.
- El tiempo es el de la captura (`observed_at`), no el del sistema. Reproducir una captura a
  0x, 1x o 4x produce las mismas señales y decisiones si el reloj simulado coincide.
- Los hechos (`facts`) salen de cifras ya calculadas. Ninguna regla inventa valores ni
  sustituye un dato ausente por cero.
- El texto visible no vive aquí: las reglas publican una `message_key`, los hechos numéricos y
  la prioridad. La traducción está en `console_messages.py`.
- `event_id` se construye de forma determinista como `regla:clave[:n]`. La misma condición en
  la misma sesión conserva su identificador entre señales y entre reproducciones.

## Reglas y ciclo de vida

Cada condición recorre este ciclo dentro de `ConditionTracker`:

```text
INACTIVA --activa--> PENDIENTE --persistencia--> ACTIVA [opened]
ACTIVA --cambio material--> ACTIVA [updated]
ACTIVA --inactiva--> CERRANDO --persistencia de salida--> INACTIVA [closed]
PENDIENTE --inactiva--> INACTIVA (sin señal)
```

- Una observación `None` o de calidad insuficiente no abre ni cierra nada.
- La persistencia se expresa en segundos o en número de observaciones consecutivas.
- La histéresis separa el umbral de entrada del de salida y evita parpadeos en el límite.
- Un cambio material se detecta con `material_key` o con una variación mínima del valor; sin
  cambio material no hay señal `updated`.
- Las reglas `one_shot` abren y cierran en la misma evaluación. Sirven para hechos puntuales
  como un cambio de clima o una bandera.
- Un cambio de `session_uid` o un flashback cierra en silencio las condiciones activas y
  reinicia el tracker: el estado de la sesión anterior no se arrastra.

### Matriz de reglas

| Regla | Categoría | Prioridad | Se abre cuando | Persistencia / histéresis |
| --- | --- | --- | --- | --- |
| `strategy_pit_recommended` | `strategy` | `strategic` | La recomendación es parar en boxes con confianza ≥ 0,5. | 2 observaciones; actualiza con 2,0 s de diferencia. |
| `pit_window_open` | `strategy` | `informational` | La ventana de parada está abierta (1 vuelta de antelación). | 1 observación. |
| `fuel_margin_low` | `fuel` | `strategic` | El margen proyectado baja de 0,5 vueltas. | 2 observaciones; sale por encima de 0,8. |
| `fuel_margin_critical` | `fuel` | `critical` | El margen proyectado llega a 0,0 vueltas. | 2 observaciones; sale por encima de 0,3. |
| `tyre_life_near_limit` | `tyres` | `strategic` | Quedan 3,0 vueltas o menos de vida de neumático. | 1 observación; actualiza con 1,0 vuelta de diferencia. |
| `tyre_degradation_abnormal` | `tyres` | `informational` | La degradación supera 1,25× la referencia de la tanda. | 2 observaciones; sale por debajo de 1,15; requiere 4 vueltas de tanda. |
| `safety_car_deployed` | `track` | `critical` | El estado de pista pasa a safety car. | Inmediata. |
| `virtual_safety_car_deployed` | `track` | `critical` | El estado de pista pasa a virtual safety car. | Inmediata. |
| `safety_car_cleared` | `track` | `strategic` | La pista vuelve a verde tras SC o VSC. | `one_shot`. |
| `red_flag` | `track` | `critical` | Se muestra bandera roja. | `one_shot`. |
| `weather_changed` | `weather` | `strategic` | Cambia la condición de clima relevante. | `one_shot`. |
| `rain_expected` | `weather` | `strategic` | La probabilidad de lluvia alcanza 50 %. | 2 observaciones; sale por debajo de 30 %; umbral inclusivo. |
| `data_stale` | `data` | `informational` | La telemetría lleva 3,0 s sin actualizarse. | Persistencia de 3,0 s. |
| `damage_detected` | `damage` | `strategic` | El daño supera el 15 % con efecto estratégico. | 1 observación; actualiza con 10 % de diferencia. |
| `rival_pit_stop` | `rivals` | `informational` | Un rival directo entra en boxes. | `one_shot`. |

## Matriz de prioridades

| Prioridad | Uso | Interrumpe | Cooldown |
| --- | --- | --- | --- |
| `critical` | Seguridad o situación que exige reacción inmediata (SC, VSC, bandera roja, combustible crítico). | Sí (`interrupt=True`), salvo que el nivel de interrupción lo impida. | Exento. |
| `strategic` | Decisión de carrera con beneficio material (parada, vida de neumático, clima, daño). | No. | 45 s global y por categoría. |
| `informational` | Contexto útil sin urgencia (ventana abierta, degradación anómala, rival en boxes). | No. | 120 s global y por categoría; además, espacio tras avisos mayores. |

El nivel de interrupción configurable actúa como filtro previo: `all`, `strategic` o
`critical`. Un aviso por debajo del nivel se registra como suprimido, no se pierde.

## La compuerta

La compuerta procesa primero las señales de mayor prioridad y conserva el orden de llegada
entre iguales. Para cada señal aplica estos controles en orden; el primero que decide gana y
queda como motivo de la decisión:

| # | Control | Resultado |
| --- | --- | --- |
| 1 | Silencio global (`muted`). | Suprime (`muted`). |
| 2 | Nivel de interrupción. | Suprime (`below_interruption_level`). |
| 3 | Cierre de un evento aplazado o ya emitido. | Cancela el aplazado (`cancelled_closed`) o suprime el cierre no anunciado (`closed_not_announced`). |
| 4 | Duplicado del mismo `event_id` en la misma evaluación. | Suprime (`duplicate`). |
| 5 | Periodo de calma para avisos informativos (primeras vueltas, tras interacción del piloto o tras un aviso mayor). | Suprime (`quiet_period`). |
| 6 | Cooldown global y por categoría; los críticos están exentos. | Aplaza un estratégico (`deferred`) o suprime un informativo (`cooldown_global` / `cooldown_category`). |
| 7 | Actualización sin cambio material o dentro del intervalo mínimo. | Suprime (`no_material_change` / `update_rate_limited`). |
| 8 | Equivalencia reciente: misma regla y misma `material_key` dentro de la ventana. | Suprime (`equivalent_recent`). |
| 9 | Arbitraje: ya se emitió un aviso de prioridad igual o mayor en esta evaluación. | Aplaza el no crítico (`lower_priority_active`). |
| 10 | Emisión. | `emit` con `emitted` o `update` con `updated`; si es crítico, `interrupt=True`. |

Las colas internas se revisan en cada evaluación:

- Un aplazado se emite cuando deja de estar bloqueado, se cancela si su condición se cierra y
  caduca a los 20 s (`deferred_expired`).
- Una actualización bloqueada por el intervalo mínimo se guarda como pendiente y se emite al
  cumplirse el intervalo, no se pierde.
- Una señal de cierre de un evento que llegó a anunciarse se emite con fase `closed` y motivo
  `closed_announced`; si nunca se anunció, se suprime en silencio.
- La cola de aplazados está acotada a 32 entradas; al desbordar se suprime la nueva
  (`deferred_queue_full`) para no crecer sin límite.
- El historial de avisos recientes se acota a 100.

## Parámetros

La configuración vive en `[events]` y `[gate]` del TOML y se valida con pydantic
(`extra="forbid"`). Estos son los valores de `config/config.example.toml`:

| Clave | Valor por defecto | Efecto |
| --- | --- | --- |
| `events.enabled` | `true` | Activa detector y compuerta. |
| `events.strategy.min_confidence` | `0.5` | Confianza mínima para recomendar parada. |
| `events.strategy.update_delta_s` | `2.0` | Cambio material en la ganancia estimada. |
| `events.pit_window.lead_laps` | `1` | Antelación con la que se abre la ventana. |
| `events.fuel.warn_laps` | `0.5` | Umbral de margen bajo. |
| `events.fuel.critical_laps` | `0.0` | Umbral de margen crítico. |
| `events.fuel.hysteresis_laps` | `0.3` | Separación entre entrada y salida. |
| `events.fuel.update_delta_laps` | `null` | Cambio material; `null` usa el valor por defecto de la regla. |
| `events.tyres.warn_laps` | `3.0` | Vida restante que abre el aviso. |
| `events.tyres.degradation_ratio` | `0.25` | Exceso sobre la referencia que se considera anómalo. |
| `events.tyres.hysteresis` | `0.10` | Margen de salida de la degradación anómala. |
| `events.tyres.min_stint_laps` | `4` | Vueltas mínimas para evaluar degradación. |
| `events.weather.rain_enter_pct` | `50.0` | Probabilidad que abre `rain_expected`. |
| `events.weather.rain_exit_pct` | `30.0` | Probabilidad que la cierra. |
| `events.weather.update_delta_pct` | `null` | Cambio material de probabilidad. |
| `events.data.stale_persistence_s` | `3.0` | Silencio que marca datos obsoletos. |
| `events.damage.warn_pct` | `15.0` | Daño que abre el aviso. |
| `events.damage.update_delta_pct` | `10.0` | Cambio material del daño. |
| `events.rivals.gap_s` | `2.0` | Hueco que define un rival directo. |
| `gate.enabled` | `true` | Activa la compuerta (si se desactiva, todo pasa). |
| `gate.global_min_gap_s` | `8.0` | Separación mínima entre avisos no críticos. |
| `gate.cooldown_s.strategic` | `45.0` | Cooldown de la categoría estratégica. |
| `gate.cooldown_s.informational` | `120.0` | Cooldown de la categoría informativa. |
| `gate.equivalence_window_s` | `90.0` | Ventana para considerar dos avisos equivalentes. |
| `gate.update_min_interval_s` | `30.0` | Intervalo mínimo entre actualizaciones. |
| `gate.defer_max_s` | `20.0` | Vida máxima de un aviso aplazado. |
| `gate.quiet_first_laps` | `1` | Vueltas iniciales sin avisos informativos. |
| `gate.informational_space_s` | `30.0` | Calma informativa tras un aviso mayor. |
| `gate.pilot_quiet_s` | `15.0` | Calma tras una interacción del piloto. |
| `gate.interruption_level` | `all` | `all`, `strategic` o `critical`. |
| `gate.muted` | `false` | Arranca en silencio si es `true`. |

## Cómo leer una decisión

Una `GateDecision` explica qué pasó con una señal concreta:

| Campo | Significado |
| --- | --- |
| `event_id` | Identificador estable de la condición (`regla:clave[:n]`). |
| `phase` | `opened`, `updated` o `closed`. |
| `outcome` | `emit`, `update`, `suppress`, `defer` o `cancel`. |
| `reason` | Motivo estable; es la clave para auditar por qué no se avisó. |
| `alert` | `AgentAlert` emitido o actualizado; `null` en supresiones, aplazamientos y cancelaciones. |
| `observed_at` | Instante simulado de la evaluación. |

Cuando hay aviso, `AgentAlert` añade `alert_id`, prioridad, categoría, `message_key`, hechos,
vuelta, `interrupt` y `supersedes` (aviso anterior al que reemplaza). `emit` significa aviso
nuevo; `update` significa que el mismo evento cambió de forma material; `suppress` es silencio
deliberado; `defer` es un aviso válido que espera; `cancel` retira algo que ya no procede.

Los motivos posibles son:

| Motivo | Resultado | Lectura |
| --- | --- | --- |
| `emitted` | `emit` | Aviso nuevo. |
| `updated` | `update` | Mismo evento con cambio material. |
| `closed_announced` | `emit` | Cierre de un evento que sí se había anunciado. |
| `muted` | `suppress` | Silencio global activo. |
| `below_interruption_level` | `suppress` | La prioridad no alcanza el nivel configurado. |
| `cancelled_closed` | `cancel` | El evento se cerró mientras estaba aplazado. |
| `closed_not_announced` | `suppress` | Se cierra un evento que nunca llegó a avisar. |
| `duplicate` | `suppress` | Misma señal repetida en la evaluación. |
| `quiet_period` | `suppress` | Calma informativa por vueltas iniciales, piloto o aviso mayor. |
| `deferred` | `defer` | Aviso válido bloqueado por cooldown. |
| `cooldown_global` | `suppress` | Cooldown informativo global. |
| `cooldown_category` | `suppress` | Cooldown informativo de la categoría. |
| `no_material_change` | `suppress` | Actualización sin diferencia relevante. |
| `update_rate_limited` | `suppress` | Actualización demasiado próxima; queda pendiente. |
| `equivalent_recent` | `suppress` | Aviso equivalente dentro de la ventana. |
| `lower_priority_active` | `defer` / `suppress` | Ya se emitió algo igual o más importante; el no crítico espera. |
| `deferred_expired` | `suppress` | El aplazado caducó sin oportunidad. |
| `deferred_queue_full` | `suppress` | Cola de aplazados llena. |

En la consola, `events show` empareja cada detección con su decisión y muestra las columnas
`Detección` y `Compuerta`. Una fila con compuerta `suppress` o `defer` no es un fallo: es el
registro de que el sistema decidió no interrumpir. `events summary` agrega esos motivos por
regla, resultado y vuelta, que es la forma rápida de revisar una sesión.

Ejemplo de lectura: `fuel_margin_low:...:1` con fase `updated` y decisión `update` /
`updated` significa que la condición ya estaba abierta y su margen cambió lo suficiente.
Si la misma fila apareciera como `suppress` / `cooldown_category`, el aviso era válido pero la
categoría estaba en enfriamiento.

## Persistencia y consulta

Desde el esquema v8 la sesión guarda ambas caras del flujo:

- `detected_events`: una fila por señal aceptada por el tracker, con regla, `event_id`, fase,
  prioridad, categoría, `message_key`, hechos, `material_key`, vuelta y tiempo simulado.
- `gate_decisions`: una fila por decisión, con `event_id`, fase, `outcome`, `reason`,
  `alert_id`, `interrupt`, vuelta y tiempo simulado.
- `sessions.gate_fingerprint`: huella canónica e independiente del orden de detecciones y
  decisiones, para comparar reproducciones sin depender de los relojes.

La CLI abre la base en solo lectura:

```bash
uv run f1-strategist events show --session 1 --db captures/sessions.db
uv run f1-strategist events show --session 1 --rule fuel_margin_low --outcome suppress --db captures/sessions.db
uv run f1-strategist events summary --session 1 --db captures/sessions.db
uv run f1-strategist events simulate CAPTURA.f1cap --from-lap 10 --to-lap 30
```

`events simulate` repite la captura en memoria y muestra detector y compuerta como si se
hubiera conducido en vivo; no escribe en la base ni llama al LLM.
