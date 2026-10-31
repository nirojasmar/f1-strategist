# Requerimientos de la aplicación --- Ingeniero de carrera IA para F1 25

**Estado:** Documento inicial de requisitos\
**Producto:** Agente local de ingeniería de carrera basado en telemetría
de F1 25

## 1. Objetivo

Construir una aplicación local que reciba telemetría de F1 25, mantenga
un estado actualizado de la carrera, evalúe alternativas estratégicas y
actúe como ingeniero de carrera virtual. El agente debe comunicarse con
el piloto casi en tiempo real, emitir avisos únicamente cuando sean
útiles y responder preguntas explícitas del piloto.

La aplicación debe funcionar localmente y permitir cambiar o evaluar
modelos de lenguaje sin acoplar la lógica estratégica a un proveedor o
modelo concreto.

## 2. Principios de diseño

1.  **Arquitectura híbrida:** cálculos, reglas, estado y simulaciones
    deben implementarse principalmente en código determinista. El LLM se
    utiliza para interpretar contexto, explicar decisiones y conversar.
2.  **Silencio por defecto:** recibir telemetría no implica generar una
    respuesta.
3.  **Decisiones basadas en evidencia:** las recomendaciones deben estar
    respaldadas por métricas, supuestos y estimaciones identificables.
4.  **Separación de responsabilidades:** recepción de datos, análisis,
    estrategia, control de eventos y conversación deben ser componentes
    independientes.
5.  **Contexto compacto:** enviar al LLM resúmenes estratégicos y datos
    relevantes, no streams completos de telemetría.
6.  **Trazabilidad:** registrar qué datos y reglas llevaron a una
    recomendación.
7.  **Tolerancia a datos incompletos:** identificar datos ausentes,
    obsoletos o inválidos y evitar conclusiones no justificadas.

## 3. Usuarios y escenarios principales

### Usuario principal

Piloto que conduce F1 25 y necesita información estratégica concisa
durante la sesión.

### Escenarios

-   El agente detecta una situación que requiere atención y comunica una
    recomendación.
-   El agente permanece en silencio cuando la situación es normal o no
    hay una acción útil.
-   El piloto pregunta por el estado de la carrera, un rival,
    combustible, neumáticos o estrategia.
-   El piloto solicita comparar alternativas, por ejemplo, parar ahora o
    continuar.
-   El sistema registra la sesión para depurar, reproducir y evaluar
    recomendaciones.

## 4. Requerimientos funcionales

### RF-01 --- Recepción de telemetría

-   Recibir paquetes UDP emitidos por F1 25.
-   Permitir configurar puerto, dirección de escucha y formato/versión
    del protocolo cuando corresponda.
-   Parsear los paquetes soportados y asociar sus datos con la sesión,
    vuelta y vehículo correctos.
-   Manejar paquetes perdidos, duplicados, desordenados o inválidos sin
    bloquear el proceso.
-   Exponer el estado de conexión y la antigüedad de la última muestra
    válida.

### RF-02 --- Normalización y estado de carrera

-   Convertir los paquetes a un modelo interno consistente.
-   Mantener el estado más reciente del coche, sesión, vuelta, rivales,
    neumáticos, combustible, daños y condiciones de carrera, según los
    datos disponibles.
-   Mantener históricos de muestras y agregados por vuelta.
-   Detectar inicio/final de sesión, cambios de vuelta, paradas y
    cambios relevantes de estado.
-   Marcar explícitamente datos desconocidos o no disponibles; no
    sustituirlos silenciosamente por valores inventados.

### RF-03 --- Cálculo de métricas

Calcular, cuando la telemetría disponible lo permita: - Consumo de
combustible por vuelta y margen estimado al final. - Edad y desgaste de
neumáticos, así como tendencia de degradación. - Ritmo por vuelta,
parciales y tendencia reciente. - Diferencia temporal con rivales y
evolución de esa diferencia. - Estimación de pérdida de tiempo en boxes
y ventana de parada. - Estado y evolución de ERS. - Condiciones
meteorológicas, temperatura, banderas y coche de seguridad. - Daños y
posibles efectos sobre el rendimiento.

Las métricas deben incluir unidades, ventana temporal, fuente y nivel de
confianza cuando sea relevante.

### RF-04 --- Motor de estrategia

-   Mantener un plan estratégico actual y sus supuestos.
-   Comparar alternativas, como mantener el plan, parar ahora o parar en
    vueltas posteriores.
-   Estimar tiempo restante de carrera por alternativa.
-   Considerar, según la información disponible, ritmo esperado,
    desgaste, compuestos, pérdida en boxes, tráfico y condiciones de
    carrera.
-   Producir una recomendación estructurada con acción, beneficio/coste
    estimado, supuestos, riesgos y confianza.
-   Indicar cuándo no hay evidencia suficiente para recomendar un
    cambio.
-   Permitir configurar parámetros y umbrales por circuito o
    condiciones, sin codificarlos como verdades universales.

### RF-05 --- Detección de eventos

-   Detectar cambios estratégicamente relevantes a partir de métricas y
    reglas.
-   Clasificar eventos por prioridad, por ejemplo: crítico, estratégico
    e informativo.
-   Aplicar umbrales, persistencia temporal, histéresis y deduplicación
    para reducir falsos positivos.
-   Diferenciar un evento nuevo de uno que continúa activo.
-   Permitir que eventos críticos ignoren el cooldown normal.
-   Evitar generar eventos por fluctuaciones normales o cambios sin
    consecuencia práctica.

### RF-06 --- Control de comunicación

-   Mantener el silencio como comportamiento predeterminado.
-   Invocar el LLM únicamente cuando exista un evento que deba
    comunicarse o una consulta del piloto.
-   Aplicar cooldown por tipo de mensaje y deduplicación por evento.
-   Evitar mensajes repetitivos como «vas bien, sigue así» si no
    contienen información accionable.
-   Priorizar avisos críticos sobre mensajes informativos.
-   Permitir configurar longitud, tono, idioma y nivel de interrupción.
-   No retrasar una respuesta explícitamente solicitada por el piloto
    debido al cooldown de avisos espontáneos.

### RF-07 --- Interacción con el piloto

-   Recibir preguntas del piloto mediante una interfaz de entrada
    definida (inicialmente texto; voz como extensión).
-   Identificar la intención de la pregunta.
-   Obtener datos actualizados mediante funciones internas y consultas
    al estado de carrera.
-   Responder preguntas como:
    -   «¿Cambiamos de estrategia?»
    -   «¿Cómo voy respecto a \[piloto\]?»
    -   «¿Cuánto combustible queda?»
    -   «¿Cuánto pueden durar estos neumáticos?»
    -   «¿Por qué estamos perdiendo tiempo?»
    -   «¿Podemos hacer undercut?»
-   Pedir aclaración cuando una referencia sea ambigua, por ejemplo, si
    hay varios pilotos posibles.
-   Distinguir hechos medidos, estimaciones y recomendaciones.
-   No inventar posiciones, diferencias, tiempos ni datos que no estén
    disponibles.

### RF-08 --- Generación de respuestas

-   Generar mensajes breves, naturales y adecuados a una comunicación de
    radio.
-   Usar cifras y resultados procedentes del estado y del motor de
    estrategia.
-   Explicar el motivo de una recomendación cuando sea útil.
-   Comunicar incertidumbre y supuestos importantes.
-   Mantener consistencia con el plan actual y los mensajes recientes.
-   Devolver una estructura validable antes de convertirla en texto o
    voz.

### RF-09 --- Registro y reproducción

-   Registrar telemetría normalizada, métricas, eventos, consultas,
    recomendaciones y respuestas.
-   Incluir marcas de tiempo y datos suficientes para reconstruir la
    secuencia de decisiones.
-   Permitir reproducir una sesión grabada a velocidad normal o
    acelerada.
-   Facilitar la comparación entre recomendaciones y evolución real de
    la carrera.
-   Permitir exportar registros para análisis y benchmark.

### RF-10 --- Configuración y observabilidad

-   Configurar modelo local, endpoint/runtime, parámetros de generación
    y prompts.
-   Configurar reglas de eventos, prioridades y cooldowns.
-   Exponer logs, errores, latencia de procesamiento y latencia de
    respuesta.
-   Mostrar el estado de conexión UDP, disponibilidad del LLM y estado
    de la sesión.
-   Permitir desactivar temporalmente la comunicación del agente sin
    detener la captura.

## 5. Requerimientos no funcionales

-   **Local-first:** la funcionalidad principal debe operar en el equipo
    del usuario.
-   **Baja latencia:** el procesamiento de telemetría no debe esperar a
    que termine una generación del LLM.
-   **Aislamiento:** una demora o caída del LLM no debe detener la
    recepción ni el almacenamiento de telemetría.
-   **Robustez:** manejar reinicios de sesión, pérdida de paquetes y
    datos parciales.
-   **Modularidad:** poder reemplazar el parser, motor estratégico,
    runtime de LLM o interfaz sin reescribir toda la aplicación.
-   **Eficiencia:** limitar el uso del modelo mediante activación por
    eventos y contextos compactos.
-   **Testabilidad:** las métricas, reglas y simulaciones deben poder
    probarse sin ejecutar el juego.
-   **Explicabilidad:** cada recomendación debe poder relacionarse con
    datos, supuestos y reglas concretas.
-   **Seguridad:** escuchar únicamente en interfaces/puertos
    configurados y no exponer servicios locales innecesariamente.

## 6. Reglas de comportamiento esenciales

1.  La telemetría se procesa continuamente; el LLM no.
2.  Una pregunta del piloto activa una ruta de respuesta independiente
    del gestor de avisos.
3.  Un evento crítico puede interrumpir; un evento informativo debe
    respetar prioridades y cooldown.
4.  El mismo evento no debe anunciarse repetidamente sin un cambio
    material.
5.  Si los datos están obsoletos o incompletos, el agente debe decirlo.
6.  El motor de estrategia entrega estimaciones y supuestos; el LLM no
    debe alterar cifras.
7.  Ninguna recomendación debe presentarse como certeza si depende de
    variables desconocidas.

## 7. Fuera del alcance inicial

-   Control automático del vehículo o ejecución de acciones dentro del
    juego.
-   Predicción perfecta del comportamiento de la IA o de otros pilotos.
-   Soporte universal para todos los juegos de F1 y todas las versiones
    del protocolo.
-   Reconocimiento y síntesis de voz en la primera iteración.
-   Modelos de estrategia entrenados con grandes datasets antes de
    disponer de una línea base funcional.

## 8. Criterios de aceptación iniciales

-   La aplicación recibe y parsea telemetría de una sesión F1 25 sin
    bloquearse.
-   El estado actual y las métricas básicas se actualizan correctamente.
-   Una sesión grabada puede reproducirse sin necesidad de abrir el
    juego.
-   El sistema permanece en silencio durante una secuencia normal sin
    eventos relevantes.
-   Un evento configurado produce un único aviso, salvo que cambie
    materialmente.
-   Un evento crítico puede saltarse el cooldown normal.
-   Una pregunta explícita del piloto obtiene respuesta aunque exista
    cooldown de avisos.
-   Las respuestas usan datos del estado actual y reconocen datos
    ausentes.
-   Una comparación de estrategias muestra alternativas, estimaciones y
    supuestos.
-   Los logs permiten explicar por qué se generó una recomendación.

## 9. Decisiones pendientes

-   Lenguaje y framework definitivos.
-   Runtime y formato del modelo local.
-   Interfaz de piloto: consola, aplicación de escritorio, overlay o
    voz.
-   Frecuencia de muestreo y paquetes concretos que se procesarán.
-   Estrategia de almacenamiento de sesiones.
-   Fórmulas iniciales de degradación y simulación de paradas.
-   Umbrales y prioridades iniciales, que deberán calibrarse mediante
    pruebas.
