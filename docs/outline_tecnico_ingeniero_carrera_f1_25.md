# Outline técnico --- Ingeniero de carrera IA local para F1 25

## 1. Visión general

Aplicación local orientada a eventos que transforma telemetría UDP de F1
25 en un estado de carrera, métricas derivadas y recomendaciones
estratégicas. Un gestor de eventos decide cuándo comunicar; el LLM local
interpreta el contexto y redacta la respuesta. Las preguntas del piloto
utilizan una ruta de consulta independiente.

### Principio arquitectónico

**El código mantiene el estado, calcula y decide cuándo consultar al
modelo. El LLM razona sobre el contexto seleccionado y se comunica.**

No se debe enviar cada batch al LLM ni depender de él para cálculos que
puedan verificarse mediante código.

## 2. Diagrama lógico

``` text
F1 25
  |
  | UDP telemetry
  v
[UDP Receiver]
  |
  v
[Packet Parser]
  |
  v
[Normalizer + Session State]
  |                     \
  |                      \--> [Session Recorder]
  v
[Metrics / Feature Engine]
  |
  v
[Strategy Engine]
  |
  v
[Event Detector]
  |
  v
[Event Gate / Priority Manager] <---- [Pilot Input]
  |                                      |
  | spontaneous event                    v
  |                               [Intent Router]
  |                                      |
  +-------------------+------------------+
                      v
              [Context Builder]
                      |
                      v
                [Local LLM]
                      |
                      v
             [Response Validator]
                      |
                      v
             [Text / Voice Output]
```

La recepción, normalización y registro deben continuar aunque el LLM
esté ocupado o no disponible.

## 3. Componentes

### 3.1 UDP Receiver

Responsabilidades: - Abrir el socket UDP configurado. - Recibir
datagramas sin bloquear el resto de la aplicación. - Añadir timestamp de
recepción. - Contabilizar paquetes recibidos, inválidos y descartados. -
Detectar periodos sin datos.

No debe contener lógica estratégica ni prompts.

### 3.2 Packet Parser

Responsabilidades: - Decodificar los paquetes de acuerdo con la
especificación de telemetría de F1 25. - Validar longitud, versión,
identificadores y rangos. - Convertir estructuras binarias a objetos
tipados. - Separar los tipos de paquete y sus datos. - Gestionar
paquetes desconocidos de forma segura.

Conviene aislar el parser detrás de una interfaz para poder actualizar
el protocolo sin modificar el resto del sistema.

### 3.3 Normalizer y Session State

Mantiene una representación unificada de: - Sesión: tipo, duración,
vuelta actual, vueltas restantes y condiciones. - Coche del usuario:
velocidad, combustible, neumáticos, ERS, daños y estado relevante. -
Rivales: identificadores, posiciones, intervalos y datos disponibles. -
Carrera: banderas, safety car, clima y otros estados. - Calidad de
datos: timestamp, antigüedad y disponibilidad de cada campo.

Mantener dos tipos de información: 1. **Estado instantáneo:** último
valor válido. 2. **Histórico:** muestras, agregados por sector/vuelta y
tendencias.

El estado debe distinguir `unknown`, `stale` y valores válidos. No
asumir que un dato ausente equivale a cero.

### 3.4 Metrics / Feature Engine

Transforma telemetría de bajo nivel en variables útiles para la
estrategia.

Ejemplos: - Consumo medio y variabilidad de combustible por vuelta. -
Margen proyectado de combustible al final. - Desgaste y edad de
neumáticos. - Ritmo limpio estimado y tendencia de degradación. -
Diferencia con rivales y cambio de intervalo. - Tiempo de parada
estimado. - Estado de ERS y oportunidades de uso. - Tendencia de daños o
pérdida de rendimiento.

Cada métrica debería tener: - Valor y unidad. - Ventana de cálculo. -
Timestamp. - Calidad o confianza. - Datos de entrada relevantes.

Evitar comparar vueltas que no sean comparables sin marcar factores como
tráfico, compuesto, edad del neumático o condiciones cambiantes.

### 3.5 Strategy Engine

Responsabilidad: comparar planes posibles y devolver resultados
estructurados.

Primera versión: - Plan actual. - Parar ahora. - Parar dentro de N
vueltas. - Mantenerse fuera bajo el plan actual.

Modelo conceptual:

``` text
estimated_total_time(plan) =
    projected_lap_times
  + pit_stop_losses
  + traffic_and_rejoin_costs
  + other_modelled_costs
```

La implementación real debe evitar contar dos veces los mismos costes y
documentar los supuestos.

Salida sugerida:

``` json
{
  "current_plan": "medium-hard",
  "recommendation": {
    "action": "PIT_NOW",
    "estimated_gain_seconds": 2.4,
    "confidence": 0.78
  },
  "alternatives": [
    {
      "action": "PIT_NOW",
      "estimated_total_time": 1234.5
    },
    {
      "action": "PIT_IN_2_LAPS",
      "estimated_total_time": 1236.9
    }
  ],
  "assumptions": [
    "No se espera lluvia durante el horizonte simulado"
  ],
  "risks": [
    "Tráfico al reincorporarse"
  ],
  "data_quality": "usable"
}
```

Los valores son ilustrativos. La salida real debe basarse en cálculos
propios.

El motor debe poder responder `NO_CHANGE` o `INSUFFICIENT_DATA`, no
forzar siempre una acción.

### 3.6 Event Detector

Evalúa métricas y estado para detectar: - Ventana de parada. - Ventaja
material de una estrategia alternativa. - Degradación anormal. -
Combustible proyectado insuficiente. - Cambio meteorológico. - Safety
car o bandera relevante. - Daño o pérdida de rendimiento. - Cambios
relevantes en la situación de un rival.

Usar: - Umbrales configurables. - Persistencia para filtrar
fluctuaciones. - Histéresis para evitar oscilaciones. - Identificadores
de evento para deduplicación. - Condiciones de cierre cuando el evento
deja de estar activo.

### 3.7 Event Gate / Priority Manager

Es la barrera que decide si una situación merece una llamada al LLM y
una posible interrupción al piloto.

Prioridades iniciales: - **Critical:** puede interrumpir y saltarse el
cooldown. - **Strategic:** requiere una decisión o acción útil; respeta
reglas de frecuencia. - **Informational:** se comunica solo si aporta
valor y hay espacio para hacerlo. - **No-op:** no genera llamada al LLM.

Controles: - Cooldown global y/o por categoría. - Deduplicación por
`event_id`. - Umbral mínimo de beneficio estratégico. - Persistencia del
evento. - Supresión de mensajes equivalentes. - Cancelación o
actualización de eventos obsoletos.

La ausencia de evento debe finalizar el flujo sin llamar al LLM.

### 3.8 Pilot Input e Intent Router

Entrada inicial: texto. La voz puede añadirse después.

El router identifica intenciones como: - `COMPARE_DRIVER` -
`ASK_STRATEGY` - `FUEL_STATUS` - `TYRE_LIFE` - `PACE_DIAGNOSIS` -
`PIT_UNDERCUT` - `GENERAL_RACE_STATE`

Después llama a funciones internas específicas. Ejemplos:

``` text
get_race_state()
compare_driver(driver_id)
estimate_fuel_margin()
estimate_tyre_life()
get_recent_pace_trend()
simulate_strategy(options)
```

La consulta explícita del piloto tiene prioridad sobre el cooldown de
mensajes espontáneos.

### 3.9 Context Builder

Construye un contexto compacto según la tarea.

Para un evento: - Tipo y prioridad del evento. - Qué cambió. - Métricas
relevantes. - Recomendación y alternativas del motor. - Incertidumbres y
datos faltantes. - Mensajes recientes relevantes.

Para una pregunta: - Intención detectada. - Resultado de la función
consultada. - Estado de carrera mínimo necesario. - Restricciones de
respuesta.

No incluir telemetría cruda irrelevante ni todo el histórico. El
histórico reside en el estado/almacenamiento y se consulta bajo demanda.

### 3.10 Local LLM y Response Validator

El LLM: - Explica recomendaciones. - Resume la situación. - Responde en
lenguaje natural. - Convierte datos estructurados en mensajes breves de
radio. - Puede señalar contradicciones o información faltante.

No debe: - Inventar cifras. - Modificar resultados numéricos del
motor. - afirmar que una estimación es certeza. - emitir órdenes si los
datos necesarios son obsoletos o insuficientes.

Se recomienda una salida estructurada antes de generar voz:

``` json
{
  "should_speak": true,
  "priority": "strategic",
  "message": "Box this lap. We estimate a 2.4-second gain.",
  "reason": "The current tyre degradation makes the alternative faster.",
  "used_facts": [
    "estimated_gain_seconds"
  ]
}
```

El validador comprueba el esquema, los hechos utilizados, la longitud y
la prioridad. El Event Gate sigue siendo la autoridad principal sobre si
llamar al modelo.

### 3.11 Session Recorder y Replay

Guardar: - Paquetes o datos normalizados, según el volumen elegido. -
Estado y métricas derivadas. - Eventos detectados y decisiones del Event
Gate. - Entradas del piloto. - Contexto enviado al modelo. - Respuestas
y latencias. - Versiones de configuración, reglas y modelo.

El replay permite probar cambios de reglas y modelos con la misma
carrera sin tener que conducir nuevamente.

## 4. Flujos principales

### 4.1 Flujo de telemetría

1.  Recibir datagrama.
2.  Parsear y validar.
3.  Normalizar y actualizar el estado.
4.  Actualizar históricos y métricas.
5.  Ejecutar el motor de estrategia cuando corresponda.
6.  Detectar eventos.
7.  Pasar eventos por Event Gate.
8.  Si no hay evento comunicable, terminar sin invocar al LLM.
9.  Si hay evento, construir contexto, generar y validar mensaje.
10. Enviar salida y registrar el resultado.

### 4.2 Flujo de pregunta del piloto

1.  Recibir la pregunta.
2.  Identificar intención y entidades (por ejemplo, piloto rival).
3.  Consultar estado o ejecutar la función necesaria.
4.  Construir contexto con resultados verificables.
5.  Generar respuesta.
6.  Validar cifras y formato.
7.  Entregar respuesta y registrar la interacción.

Este flujo no debe ser bloqueado por el cooldown de alertas espontáneas.

## 5. Contratos de datos sugeridos

Separar modelos internos de los formatos de paquetes UDP.

Entidades principales: - `TelemetryPacket` - `SessionState` -
`CarState` - `OpponentState` - `LapMetrics` - `StrategyPlan` -
`StrategyEvaluation` - `RaceEvent` - `PilotQuery` - `AgentResponse`

Cada objeto debería incluir identificadores y timestamps cuando aplique.

### Ejemplo de snapshot estratégico

``` json
{
  "session": {
    "lap": 24,
    "laps_remaining": 18,
    "safety_car": false
  },
  "player": {
    "position": 4,
    "tyre_compound": "medium",
    "tyre_age_laps": 12,
    "fuel_margin_laps": 0.8,
    "pace_trend": "degrading"
  },
  "opponent": {
    "name": "Hamilton",
    "gap_seconds": 1.8,
    "gap_trend": "closing"
  },
  "strategy": {
    "current_plan": "medium-hard",
    "best_alternative": "pit_now",
    "estimated_gain_seconds": 2.4,
    "confidence": 0.78
  },
  "trigger": {
    "type": "strategy_change",
    "reason": "material_advantage"
  }
}
```

Ejemplo ilustrativo; los campos finales dependerán de los paquetes y
métricas implementados.

## 6. Consideraciones de rendimiento y concurrencia

-   El loop de UDP debe ser ligero y no esperar al LLM.
-   Las llamadas al modelo deben ejecutarse en un worker/tarea
    independiente.
-   Mantener una cola acotada para evitar acumular mensajes obsoletos.
-   Si llega un evento nuevo mientras se genera una respuesta, definir
    si reemplaza, cancela o espera a la anterior según prioridad.
-   Las preguntas del piloto deben tener una ruta prioritaria.
-   Medir latencia desde la recepción del dato relevante hasta la salida
    de voz/texto.
-   Evitar ejecutar simulaciones costosas en cada paquete; ejecutarlas
    al cambiar métricas relevantes o a intervalos adecuados.

## 7. Pruebas

-   Tests unitarios de parser y normalizador.
-   Tests de métricas con muestras conocidas.
-   Tests de estrategia con escenarios sintéticos.
-   Tests de umbrales, persistencia, histéresis y cooldown.
-   Tests de deduplicación y prioridades.
-   Tests de preguntas con datos presentes, ausentes y obsoletos.
-   Replay de sesiones grabadas.
-   Evaluación de calidad de respuestas y consistencia numérica.
-   Pruebas de carga y latencia con el modelo local.

## 8. Decisiones técnicas por resolver

-   Lenguaje principal y modelo de concurrencia.
-   Biblioteca/parser UDP y compatibilidad con la versión del juego.
-   Runtime del LLM local y protocolo de integración.
-   Formato de almacenamiento de sesiones.
-   Interfaz de usuario y salida de audio.
-   Frecuencia de cálculo de estrategia.
-   Algoritmos y supuestos para degradación, tráfico y tiempo de parada.
-   Política de cancelación de generaciones obsoletas.
