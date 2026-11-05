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

-   Crear la estructura inicial del repositorio con el stack acordado
    (Python 3.12+, `uv`, `pyproject.toml`, `ruff`, `pyright`, `pytest`) y
    CI en GitHub Actions sobre Windows y Linux.
-   Obtener la especificación UDP oficial de F1 25 (2026 Season Pack) y
    fijar el formato UDP del juego (2025 o 2024) que se procesará.
-   Revisar la especificación UDP correspondiente a F1 25 y la
    configuración del juego.
-   Confirmar paquetes disponibles y campos necesarios.
-   Definir cómo se ejecutará el modelo local. La invocación será por
    una API HTTP compatible con OpenAI mediante un adaptador
    intercambiable.
-   Primera interfaz definida: consola. La GUI queda planificada para
    una fase posterior (Fase 8).
-   Definir las dos topologías de despliegue (mismo PC y red local) y
    qué se configura en cada una.
-   Definir desde el inicio una interfaz estable entre el núcleo y la
    consola, para no acoplar la lógica a la presentación.
-   Adoptar la convención «sin textos codificados»: mensajes visibles
    mediante claves y catálogo, aunque al inicio exista un solo idioma.
-   Crear configuración externa para dirección y puerto de escucha,
    filtro de IP de origen, endpoint del modelo, logging y parámetros.

### Entregables

-   Repositorio con estructura inicial.
-   Documento de arquitectura y decisiones técnicas.
-   Aplicación mínima que abre el receptor UDP.
-   Registro de paquetes recibidos y errores.
-   Guía de configuración para ambas topologías: ajustes de telemetría
    del juego (IP de destino, puerto, formato, frecuencia) y regla de
    firewall en el PC de la aplicación.

### Criterio de salida

Se reciben paquetes de una sesión real y se confirma que el parser
reconoce los tipos de paquete requeridos, tanto con el juego y la
aplicación en el mismo PC como con la aplicación en otro PC de la red
local.

------------------------------------------------------------------------

## Fase 1 --- Receptor, parser y estado de carrera

### Trabajo

-   Implementar el receptor UDP con hilo dedicado, socket bloqueante y cola acotada.
-   Implementar parser por tipo de paquete.
-   Escribir el parser propio con `struct` o `ctypes`, detrás de una
    interfaz, procesando solo los tipos de paquete necesarios. Contrastar
    los layouts con la librería `F1Game.UDP` como referencia.
-   Admitir formato UDP 2025, año 25, versión mayor 1 y paquete v1.
    Aceptar cambios de versión menor del juego si coincide el tamaño
    exacto, con un aviso mientras no estén verificados. Los tipos
    `Session`, `LapData`, `Participants`,
    `CarStatus`, `CarDamage`, `TyreSets`, `SessionHistory`, `Event` y
    `FinalClassification`. Rechazar otros formatos, versiones y tipos con
    un diagnóstico claro; mapear enums desconocidos a `unknown`.
-   Contrastar los tamaños de esos paquetes con la captura real:
    `Session` 753, `LapData` 1285, `Participants` 1284, `CarStatus` 1239,
    `CarDamage` 1041, `TyreSets` 231, `SessionHistory` 1460, `Event` 45
    y `FinalClassification` 1042 bytes.
-   Medir y mostrar por tipo de paquete huecos estimados, duplicados y
    paquetes fuera de orden usando `overall_frame_identifier`, una
    cadencia aprendida por tipo y retención limitada por sesión.
-   Mantener la recepción en un hilo: con la carga observada, migrar a
    `asyncio` no aporta valor. Los huecos son estimaciones, no pérdidas
    confirmadas.
-   Validar versiones, tamaños y rangos.
-   Normalizar los datos a modelos internos.
-   Mantener estado de sesión, coche y rivales.
-   Detectar cambios de vuelta y de sesión.
-   Gestionar paquetes perdidos, duplicados y datos obsoletos.
-   Guardar una primera captura de telemetría.
-   Registrar la IP de origen, aplicar el filtro de orígenes y mostrar
    en la consola el estado de conexión con diagnóstico de ausencia de
    datos.

### Entregables

-   Módulo de recepción y parsing.
-   Modelos internos tipados.
-   Estado de carrera consultable.
-   Logs de calidad y antigüedad de datos.
-   Una o más capturas de sesiones reales.

### Pruebas

-   Parser con fixtures anonimizados de captura real y pruebas de
    propiedades (`hypothesis`) con datagramas y cuerpos corruptos.
-   Paquetes inválidos o incompletos.
-   Cambio de sesión y reinicio.
-   Continuidad del estado pese a paquetes perdidos.
-   Recepción en loopback y desde otro PC de la red local, incluyendo
    filtro de origen y puerto ocupado.

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

Criterio medido sobre el fixture real de Monza
(`tests/fixtures/real/monza-13-laps.f1cap.gz`, 13 vueltas y 20 coches
clasificados): **verificado**. Las métricas básicas coinciden con los
datos observables del juego dentro de estas tolerancias y no presentan
saltos injustificados:

-   Tiempos y sectores oficiales, mejor vuelta, vueltas completadas,
    paradas, edad de neumático, intervalos y suma de vueltas frente al
    tiempo total de carrera: exactos, salvo la suma frente al tiempo
    total, que admite 0,05 s.
-   Consumo de combustible: ±0,05 kg/lap frente al cálculo independiente
    del tanque.
-   Vueltas restantes: ±0,05 vueltas.
-   Tendencias: sin saltos injustificados; el salto máximo entre
    publicaciones consecutivas se limita a 0,5 s/lap para ritmo y
    0,1 %/lap para desgaste. Las tendencias de intervalo se publican,
    pero no se comparan contra un umbral fijo porque el rival puede
    cambiar.
-   Replay completo: 36 471 paquetes leídos y parseados, sin
    truncamiento ni rechazos. Coste medio de `MetricsEngine.apply`:
    0,225 ms, por debajo de 1 ms.

Las tolerancias se comprueban en `tests/test_metrics_real_capture.py`.
La validación usa un fixture anonimizado y no cubre todavía todas las
condiciones de carrera (por ejemplo, lluvia o safety car).

------------------------------------------------------------------------

## Fase 3 --- Grabación y replay

### Trabajo

-   Diseñar el formato de almacenamiento: captura binaria cruda
    (timestamp, longitud y payload) para el replay, y SQLite para
    métricas, eventos, consultas y llamadas al LLM.
-   Registrar datos normalizados, métricas y eventos.
-   Implementar reproducción de sesiones grabadas.
-   Permitir pausar, acelerar y repetir una sesión.
-   Asociar cada reproducción con la versión del parser y configuración.
-   Añadir un modo de reemisión por UDP hacia una dirección y puerto
    configurables, para probar el receptor y el despliegue en red local
    sin abrir el juego.

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
-   Implementar el adaptador como cliente HTTP de una API compatible
    con OpenAI, con salida estructurada validada con `pydantic`.
-   Medir la latencia de primer token y total con el modelo elegido,
    también con el juego en ejecución.
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

-   Añadir entrada de texto en la consola, a través de la interfaz del
    núcleo.
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

## Fase 8 --- Comunicación de voz e interfaz gráfica

### Trabajo

-   Elegir motor local o integración para speech-to-text.
-   Elegir motor local o integración para text-to-speech.
-   Añadir push-to-talk o activación por voz.
-   Definir cómo se interrumpen o encolan mensajes.
-   Añadir control de volumen, idioma y velocidad.
-   Seguir la ruta acordada para la GUI (web local), en tres pasos:
    1.  **API del núcleo y dashboard mínimo:** exponer la interfaz del
        núcleo como API local (HTTP y WebSocket) con contrato versionado
        (snapshot y cambios con número de secuencia, frecuencia
        limitada) y un dashboard web con estado de conexión, carrera,
        eventos, estrategia, avisos y entrada de preguntas.
    2.  **Pulido visual y diseño para carrera:** pocos datos grandes y
        legibles durante la conducción, paneles detallados para antes y
        después, y tipos de TypeScript generados desde el esquema.
    3.  **Empaquetado de escritorio (opcional):** Tauri o Electron con el
        núcleo como proceso auxiliar.
-   Definir el acceso por red: loopback por defecto; abrir la API a la
    red local (por ejemplo, tablet o segundo PC como pantalla) solo de
    forma explícita y con token.
-   Evaluar un overlay solo si el juego corre en ventana sin bordes. No
    se inyecta nada en el proceso del juego; la segunda pantalla es la
    opción preferida.
-   Evaluar el uso de voz o segunda pantalla cuando la aplicación corre
    en otro PC, donde escribir durante la conducción no es práctico.

### Entregables

-   Entrada por voz.
-   Salida hablada.
-   API local con contrato versionado y dashboard web mínimo de
    carrera.
-   Gestión de interrupciones.

### Criterio de salida

El piloto puede hacer preguntas y recibir mensajes breves sin que la
interacción afecte la captura de telemetría. La GUI puede abrirse,
cerrarse y reconectarse sin afectar a la captura ni al agente.

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

## Fase 10 --- Localización (futuro): inglés y español

Fase posterior al MVP. La preparación (claves de mensaje, catálogos,
registros neutros al idioma) se hace desde la Fase 0 para que esta fase
no requiera refactorizar el núcleo.

### Trabajo

-   Crear catálogos de mensajes en inglés y español con `gettext` (y
    `Babel` para formatos), con idioma de respaldo.
-   Separar los ajustes de idioma de interfaz, comunicación del agente,
    entrada del piloto y voz.
-   Versionar prompts por idioma y definir un glosario de terminología
    de carrera por idioma.
-   Extender el router de intenciones para preguntas en ambos idiomas.
-   Añadir al validador la comprobación del idioma de la respuesta.
-   Localizar el formato de números y unidades en la presentación.
-   Adaptar los motores de voz (STT/TTS) a ambos idiomas, si la Fase 8
    ya está implementada.

### Pruebas

-   Detección de claves faltantes y uso del idioma de respaldo.
-   Mismo evento en inglés y español con las mismas cifras.
-   Preguntas equivalentes del piloto en ambos idiomas con la misma
    intención detectada.
-   Reproducir una misma sesión grabada en ambos idiomas.

### Entregables

-   Catálogos en inglés y español.
-   Prompts y glosarios versionados por idioma.
-   Suite de evaluación de respuestas por idioma.

### Criterio de salida

El piloto puede usar la aplicación completa en inglés o en español sin
diferencias en datos, recomendaciones ni cifras.

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
9.  Interfaz de consola como única interfaz del MVP.
10. Ejecución en el mismo PC del juego o en otro PC de la red local,
    cambiando solo la configuración.

Dejar voz, GUI, overlay sofisticado, localización completa,
optimización avanzada y predicción aprendida para iteraciones
posteriores. Aun así, mantener desde el MVP la interfaz estable del
núcleo y las claves de mensaje para no bloquear esas extensiones.

## Riesgos y mitigaciones

| Riesgo | Mitigación |
| --- | --- |
| El parser interpreta mal una versión del protocolo | Validar contra la especificación y usar fixtures de paquetes reales. |
| Métricas ruidosas provocan recomendaciones erróneas | Ventanas, filtros, calidad de datos y pruebas con replay. |
| El motor estima mal la degradación | Empezar con modelos simples y medir error por compuesto/circuito. |
| El agente habla demasiado | Event Gate determinista, cooldown, persistencia y deduplicación. |
| El LLM inventa datos | Contextos estructurados, funciones de consulta y validador de respuestas. |
| El LLM tarda demasiado | Llamadas asíncronas, colas acotadas y mensajes críticos priorizados. |
| Una respuesta queda obsoleta mientras se genera | Validar timestamps y cancelar o descartar generaciones antiguas. |
| El alcance crece demasiado | Mantener el MVP centrado en estrategia de parada y consultas básicas. |
| La telemetría no llega al PC de la red local (IP, firewall, Wi-Fi) | Diagnóstico en el estado de conexión, guía de configuración, filtro de origen opcional, pruebas con reemisión UDP y red cableada recomendada. |
| La aplicación y el LLM compiten con el juego por CPU/GPU en el mismo PC | Medir el impacto en el juego, endpoint del LLM configurable hacia otra máquina y modelos más ligeros. |
| La lógica se acopla a la consola y dificulta la futura GUI | Interfaz estable del núcleo, adaptadores sin lógica y pruebas del núcleo sin interfaz. |
| Textos y prompts codificados dificultan la localización | Claves de mensaje, catálogos por idioma, registros neutros al idioma y pruebas de i18n desde el inicio. |
| La aplicación queda expuesta en la red local sin control | Loopback por defecto, apertura explícita, filtro de IP de origen y API/GUI remota desactivadas por defecto. |
| No hay un parser verificado en Python para F1 25 | Parser propio contra la especificación oficial, capturas reales como fixtures, contraste con `F1Game.UDP` y pruebas de propiedades. |
| La GUI y el núcleo se desincronizan | Esquema versionado, tipos generados, pruebas de contrato y de reconexión. |
| La GUI es demasiado compleja para usarla durante la carrera | Pocos datos grandes en carrera, voz como canal principal y segunda pantalla en lugar de overlay. |
| El 2026 Season Pack cambia la especificación UDP respecto a F1 25 | Basar el parser en la especificación del Season Pack, guardar versión de juego y de paquete en cada captura y avisar ante formatos no soportados. |

## Primer sprint sugerido

Un sprint inicial razonable podría terminar con una demo sin LLM:

-   La aplicación recibe telemetría UDP.
-   Muestra por consola vuelta, posición, combustible, compuesto y
    datos básicos del coche.
-   Funciona con el juego y la aplicación en el mismo PC y con la
    aplicación en otro PC de la red local, cambiando solo la
    configuración.
-   Calcula una tendencia simple de ritmo.
-   Registra la sesión.
-   Puede reproducir la captura.
-   No se bloquea ante paquetes inválidos o ausencia temporal de datos.

**Definición de terminado:** la captura y el estado son suficientemente
fiables como para empezar a construir decisiones encima. No integrar
todavía la generación de lenguaje; primero validar los datos que el
futuro ingeniero utilizará.
