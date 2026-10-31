# Roadmap de construcción --- Ingeniero de carrera IA local para F1 25

## Objetivo del roadmap

Construir de forma incremental un ingeniero de carrera funcional,
medible y ampliable. La prioridad inicial no es tener muchas funciones,
sino validar el flujo completo: telemetría fiable, métricas correctas,
eventos útiles, silencio cuando corresponde y respuestas fundamentadas.

## Principios para ejecutar el roadmap

-   Entregar incrementos pequeños y verificables.
-   No integrar el LLM hasta que el estado y las métricas básicas sean
    confiables.
-   Mantener una sesión grabada de referencia para repetir pruebas.
-   Medir latencia y calidad desde el principio.
-   Mantener el motor estratégico independiente del modelo de lenguaje.
-   No intentar resolver todos los compuestos, circuitos y condiciones
    en la primera versión.

------------------------------------------------------------------------

## Fase 0 --- Definición del proyecto y validación del entorno

### Trabajo

-   Elegir lenguaje y estructura inicial del repositorio.
-   Revisar la especificación UDP correspondiente a F1 25 y la
    configuración del juego.
-   Confirmar paquetes disponibles y campos necesarios.
-   Definir cómo se ejecutará el modelo local y cómo se invocará desde
    la aplicación.
-   Definir una primera interfaz: logs/terminal o pequeña interfaz
    local.
-   Crear configuración externa para puerto UDP, modelo, logging y
    parámetros.

### Entregables

-   Repositorio con estructura inicial.
-   Documento de arquitectura y decisiones técnicas.
-   Aplicación mínima que abre el receptor UDP.
-   Registro de paquetes recibidos y errores.

### Criterio de salida

Se reciben paquetes de una sesión real y se confirma que el parser
reconoce los tipos de paquete requeridos.

------------------------------------------------------------------------

## Fase 1 --- Receptor, parser y estado de carrera

### Trabajo

-   Implementar receptor UDP no bloqueante.
-   Implementar parser por tipo de paquete.
-   Validar versiones, tamaños y rangos.
-   Normalizar los datos a modelos internos.
-   Mantener estado de sesión, coche y rivales.
-   Detectar cambios de vuelta y de sesión.
-   Gestionar paquetes perdidos, duplicados y datos obsoletos.
-   Guardar una primera captura de telemetría.

### Entregables

-   Módulo de recepción y parsing.
-   Modelos internos tipados.
-   Estado de carrera consultable.
-   Logs de calidad y antigüedad de datos.
-   Una o más capturas de sesiones reales.

### Pruebas

-   Parser con fixtures de paquetes.
-   Paquetes inválidos o incompletos.
-   Cambio de sesión y reinicio.
-   Continuidad del estado pese a paquetes perdidos.

### Criterio de salida

La aplicación puede mantener un estado razonablemente consistente
durante una sesión sin bloquearse.

------------------------------------------------------------------------

## Fase 2 --- Métricas e histórico

### Trabajo

Implementar primero métricas que puedan calcularse con fiabilidad: -
Vuelta actual y vueltas restantes. - Consumo de combustible por vuelta y
margen proyectado. - Edad de neumáticos y desgaste disponible. - Tiempo
por vuelta y tendencia de ritmo. - Intervalos con rivales y tendencia. -
Estado de ERS y daños, si los paquetes lo permiten.

Añadir: - Agregación por vuelta y sector. - Ventanas móviles. -
Detección de vueltas no comparables. - Calidad/confianza y antigüedad
por métrica. - Exportación de métricas para análisis.

### Entregables

-   Metrics Engine.
-   Histórico por vuelta.
-   Snapshot estratégico en JSON.
-   Tests unitarios con datos de ejemplo.

### Criterio de salida

Las métricas básicas coinciden con los datos observables del juego
dentro de tolerancias definidas y no presentan saltos injustificados.

------------------------------------------------------------------------

## Fase 3 --- Grabación y replay

### Trabajo

-   Diseñar formato de almacenamiento.
-   Registrar datos normalizados, métricas y eventos.
-   Implementar reproducción de sesiones grabadas.
-   Permitir pausar, acelerar y repetir una sesión.
-   Asociar cada reproducción con la versión del parser y configuración.

### Entregables

-   Session Recorder.
-   Replay Runner.
-   Dataset inicial de escenarios reales.
-   Herramientas básicas de inspección.

### Criterio de salida

Es posible reproducir la misma sesión de forma determinista para probar
cambios sin volver a conducir.

Esta fase es importante: permitirá comparar reglas y modelos usando
exactamente las mismas condiciones.

------------------------------------------------------------------------

## Fase 4 --- Motor de estrategia mínimo

### Alcance recomendado para la primera versión

Limitar el problema a una decisión concreta: **¿mantener el plan o parar
ahora/en una ventana próxima?**

### Trabajo

-   Definir el modelo de tiempo restante.
-   Estimar ritmo futuro con una línea base sencilla.
-   Estimar pérdida de tiempo en boxes.
-   Considerar edad/desgaste de neumáticos.
-   Considerar tráfico y coste de reincorporación cuando haya datos
    suficientes.
-   Comparar plan actual con pocas alternativas.
-   Devolver estimaciones, supuestos, riesgos y confianza.
-   Permitir `NO_CHANGE` e `INSUFFICIENT_DATA`.

No comenzar con un optimizador complejo ni con un modelo predictivo
entrenado. Primero establecer una línea base transparente.

### Entregables

-   Strategy Engine inicial.
-   Evaluación estructurada de alternativas.
-   Escenarios sintéticos y pruebas de regresión.
-   Registro de las predicciones realizadas.

### Criterio de salida

Ante escenarios controlados, el motor compara correctamente las
alternativas y explica qué supuestos afectan el resultado.

------------------------------------------------------------------------

## Fase 5 --- Event Detector y Event Gate

### Trabajo

Implementar eventos iniciales: - Ventana de parada. - Ventaja
estratégica material. - Combustible proyectado insuficiente. -
Degradación anormal. - Safety car/bandera/cambio de clima relevante. -
Daño con efecto estratégico.

Implementar controles: - Prioridad. - Umbrales configurables. -
Persistencia e histéresis. - Cooldown. - Deduplicación. - Cierre de
eventos. - Excepción de cooldown para eventos críticos.

### Entregables

-   Event Detector.
-   Event Gate.
-   Matriz de prioridades.
-   Configuración de reglas.
-   Tests de secuencias normales y excepcionales.

### Prueba clave

Reproducir una sesión y comprobar: - No hay mensajes durante periodos
normales. - Una condición persistente no genera avisos repetidos. - Un
cambio material genera un nuevo aviso. - Un evento crítico puede
interrumpir. - El cooldown no bloquea preguntas del piloto (se validará
junto con la fase de conversación).

### Criterio de salida

La aplicación distingue entre telemetría normal y situaciones que
justifican una intervención sin depender del LLM.

------------------------------------------------------------------------

## Fase 6 --- Integración del LLM local

### Trabajo

-   Crear una interfaz desacoplada para el runtime del modelo.
-   Configurar modelo, parámetros y timeout.
-   Definir prompts separados para mensajes espontáneos y preguntas.
-   Construir contextos compactos y específicos.
-   Exigir salida estructurada.
-   Validar esquema, cifras, prioridad y longitud.
-   Manejar errores, timeout y generaciones obsoletas.
-   Registrar prompt/contexto, versión del modelo, respuesta y latencia.

### Comportamiento obligatorio

-   Si no hay evento comunicable, no llamar al LLM.
-   Si hay un evento, explicar qué ocurre y qué acción se recomienda.
-   No inventar números.
-   Reconocer incertidumbre y datos ausentes.
-   Mantener mensajes breves, naturales y accionables.

### Entregables

-   LLM Adapter.
-   Context Builder.
-   Response Validator.
-   Prompts versionados.
-   Suite de evaluación de respuestas.

### Criterio de salida

El modelo genera mensajes consistentes con los datos y la recomendación
estructurada, sin convertirse en el responsable de los cálculos
estratégicos.

------------------------------------------------------------------------

## Fase 7 --- Preguntas del piloto

### Trabajo

-   Añadir entrada de texto.
-   Implementar Intent Router.
-   Crear herramientas/funciones internas:
    -   `get_race_state`
    -   `compare_driver`
    -   `estimate_fuel_margin`
    -   `estimate_tyre_life`
    -   `get_recent_pace_trend`
    -   `simulate_strategy`
-   Resolver nombres de pilotos a identificadores.
-   Gestionar preguntas ambiguas y datos no disponibles.
-   Priorizar consultas explícitas sobre avisos espontáneos.

### Casos de prueba

-   «¿Cambiamos de estrategia?»
-   «¿Cómo voy respecto a \[piloto\]?»
-   «¿Cuánto combustible queda?»
-   «¿Cuánto aguantan estos neumáticos?»
-   «¿Por qué estamos perdiendo tiempo?»
-   «¿Podemos hacer undercut?»

### Entregables

-   Interfaz de consulta.
-   Router de intenciones.
-   Funciones de consulta.
-   Respuestas basadas en datos verificables.

### Criterio de salida

Las preguntas se responden con datos actuales, y el agente distingue
mediciones, estimaciones y recomendaciones.

------------------------------------------------------------------------

## Fase 8 --- Comunicación de voz e interfaz

### Trabajo

-   Elegir motor local o integración para speech-to-text.
-   Elegir motor local o integración para text-to-speech.
-   Añadir push-to-talk o activación por voz.
-   Definir cómo se interrumpen o encolan mensajes.
-   Añadir control de volumen, idioma y velocidad.
-   Mostrar estado de conexión, eventos y estrategia en una interfaz
    sencilla.

### Entregables

-   Entrada por voz.
-   Salida hablada.
-   Interfaz mínima de carrera.
-   Gestión de interrupciones.

### Criterio de salida

El piloto puede hacer preguntas y recibir mensajes breves sin que la
interacción afecte la captura de telemetría.

------------------------------------------------------------------------

## Fase 9 --- Calibración y evaluación

### Trabajo

-   Reproducir múltiples sesiones y condiciones.
-   Comparar predicciones con resultados reales.
-   Medir error de proyección de ritmo y combustible.
-   Medir utilidad de las recomendaciones.
-   Revisar falsos positivos y eventos omitidos.
-   Medir mensajes por vuelta, mensajes duplicados y latencia.
-   Comparar modelos locales en las mismas tareas y contextos.
-   Ajustar reglas y parámetros sin cambiar simultáneamente demasiadas
    variables.

### Métricas de evaluación sugeridas

-   Tasa de paquetes válidos.
-   Porcentaje de métricas disponibles y frescas.
-   Error de consumo de combustible proyectado.
-   Error de ritmo proyectado.
-   Diferencia estimada frente al tiempo real de estrategia, cuando sea
    medible.
-   Precisión de eventos y tasa de falsos avisos.
-   Número de mensajes no accionables.
-   Latencia de detección a mensaje.
-   Latencia de pregunta a respuesta.
-   Porcentaje de respuestas con cifras respaldadas por datos.

### Criterio de salida

Existe una línea base cuantitativa para decidir qué mejorar: parser,
métricas, estrategia, gestor de eventos o modelo.

------------------------------------------------------------------------

## Orden recomendado del MVP

Para evitar dispersión, el primer MVP debería incluir solamente:

1.  Receptor UDP y parser.
2.  Estado de carrera.
3.  Combustible, neumáticos, ritmo e intervalos.
4.  Grabación y replay.
5.  Comparación básica de parada ahora vs. continuar.
6.  Event Gate con silencio por defecto.
7.  LLM local para explicar un evento.
8.  Preguntas de texto sobre estado y estrategia.

Dejar voz, overlay sofisticado, optimización avanzada y predicción
aprendida para iteraciones posteriores.

## Riesgos y mitigaciones

  -----------------------------------------------------------------------
  Riesgo                              Mitigación
  ----------------------------------- -----------------------------------
  El parser interpreta mal una        Validar contra la especificación y
  versión del protocolo               usar fixtures de paquetes reales.

  Métricas ruidosas provocan          Ventanas, filtros, calidad de datos
  recomendaciones erróneas            y pruebas con replay.

  El motor estima mal la degradación  Empezar con modelos simples y medir
                                      error por compuesto/circuito.

  El agente habla demasiado           Event Gate determinista, cooldown,
                                      persistencia y deduplicación.

  El LLM inventa datos                Contextos estructurados, funciones
                                      de consulta y validador de
                                      respuestas.

  El LLM tarda demasiado              Llamadas asíncronas, colas acotadas
                                      y mensajes críticos priorizados.

  Una respuesta queda obsoleta        Validar timestamps y cancelar o
  mientras se genera                  descartar generaciones antiguas.

  El alcance crece demasiado          Mantener el MVP centrado en
                                      estrategia de parada y consultas
                                      básicas.
  -----------------------------------------------------------------------

## Primer sprint sugerido

Un sprint inicial razonable podría terminar con una demo sin LLM:

-   La aplicación recibe telemetría UDP.
-   Muestra vuelta, posición, combustible, compuesto y datos básicos del
    coche.
-   Calcula una tendencia simple de ritmo.
-   Registra la sesión.
-   Puede reproducir la captura.
-   No se bloquea ante paquetes inválidos o ausencia temporal de datos.

**Definición de terminado:** la captura y el estado son suficientemente
fiables como para empezar a construir decisiones encima. No integrar
todavía la generación de lenguaje; primero validar los datos que el
futuro ingeniero utilizará.
