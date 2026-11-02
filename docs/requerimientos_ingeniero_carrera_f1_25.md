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

Por «localmente» se entiende que la aplicación puede ejecutarse en el
mismo PC donde corre el juego o en otro PC de la red local conectado a
él (ver RF-11), sin depender de servicios en la nube. La primera
interfaz será la consola; se prevé una interfaz gráfica (RF-12) y, más
adelante, localización a inglés y español (RF-13).

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
8.  **Despliegue flexible:** el mismo núcleo debe poder ejecutarse en el
    PC del juego o en otro PC de la red local, cambiando solo la
    configuración.
9.  **Núcleo independiente de la interfaz:** la lógica no depende de la
    consola. La consola es la primera interfaz y la futura GUI debe
    consumir el mismo núcleo sin reescribirlo.
10. **Preparado para localización:** ningún texto visible para el
    usuario ni prompt debe estar codificado dentro de la lógica, aunque
    la localización completa llegue después.

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
-   El piloto ejecuta la aplicación en el mismo PC del juego o en un
    segundo PC de la red local (por ejemplo, un portátil junto al
    simulador) y obtiene el mismo comportamiento funcional.
-   El piloto usa la aplicación desde la consola y, en el futuro, desde
    una interfaz gráfica, y en inglés o español.

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
-   Permitir configurar longitud, tono, idioma (ver RF-13) y nivel de
    interrupción.
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

### RF-11 --- Despliegue en el mismo PC o en la red local

-   Soportar dos topologías con el mismo código y el mismo
    comportamiento funcional:
    1.  **Local:** el juego y la aplicación se ejecutan en el mismo PC.
    2.  **Red local:** el juego se ejecuta en un PC y la aplicación en
        otro PC conectado a la misma red local.
-   Cambiar de topología debe requerir solo configuración (dirección y
    puerto de escucha, filtros, endpoint del LLM), no ramas de código ni
    builds distintos.
-   Dirección de escucha por defecto: solo loopback (`127.0.0.1`). Para
    recibir desde otro PC, el usuario debe indicar explícitamente la
    interfaz de red o `0.0.0.0`.
-   Permitir un filtro opcional de IP de origen para aceptar paquetes
    solo del PC del juego.
-   Documentar la configuración necesaria en cada caso: en el juego (IP
    de destino de la telemetría, puerto, formato y frecuencia de envío)
    y en el PC de la aplicación (regla de firewall para el puerto UDP).
-   Mostrar en el estado de conexión (RF-10) la dirección y el puerto de
    escucha, la IP de origen de los paquetes, la antigüedad de la última
    muestra y los contadores de paquetes válidos, inválidos y perdidos.
-   Cuando no lleguen datos, orientar el diagnóstico con causas
    probables (IP de destino incorrecta en el juego, firewall, puerto
    ocupado, telemetría desactivada en el juego).
-   Tolerar la latencia, el jitter y la pérdida de paquetes propios de
    una red local usando el manejo ya definido en RF-01. Se recomienda
    conexión cableada en el PC de la aplicación.
-   No depender de relojes sincronizados entre PCs: la recepción se
    marca con el reloj del PC de la aplicación y el orden se apoya en el
    tiempo de sesión del juego.
-   Permitir que el endpoint del LLM sea local o esté en otra máquina de
    la red local, por ejemplo, para evitar que el modelo compita por
    CPU/GPU con el juego cuando ambos corren en el mismo PC.
-   Cuando la aplicación corra en un PC distinto, la entrada y la salida
    del piloto ocurren en ese PC. La entrada de texto durante la
    conducción es poco práctica, por lo que esta topología aumenta el
    valor de la voz (Fase 8) o de una segunda pantalla.

### RF-12 --- Interfaz de usuario: consola primero, GUI después

-   La primera versión se ejecuta en consola (CLI o TUI) y cubre:
    estado de conexión y sesión, avisos del agente, entrada de preguntas
    por texto, logs y configuración básica.
-   El núcleo expone una interfaz estable y documentada (comandos,
    consultas y flujo de eventos/mensajes) independiente de la
    presentación. La consola es un adaptador sobre esa interfaz y no
    contiene lógica estratégica.
-   El núcleo debe poder ejecutarse sin interfaz gráfica.
-   Se espera crear una GUI en una etapa posterior. La ruta acordada es
    una interfaz web local que se conecta al núcleo mediante una API
    local (HTTP y WebSocket); el núcleo no depende de ella. Debe
    consumir la misma interfaz que la consola y mostrar, como mínimo:
    estado de conexión, estado de carrera, eventos, estrategia actual,
    avisos y entrada de preguntas.
-   El contrato de la API es versionado y se publica como esquema (por
    ejemplo, JSON Schema). Los tipos de la GUI se generan a partir de ese
    esquema para que ambos lados no se desincronicen.
-   Al conectarse o reconectarse, la GUI recibe el estado completo
    (snapshot) y después cambios incrementales con número de secuencia.
    Abrir o cerrar la GUI no debe afectar al núcleo.
-   La GUI recibe estados resumidos a una frecuencia limitada (del orden
    de 10 a 20 Hz), no cada paquete de telemetría.
-   La API escucha solo en loopback por defecto. Abrirla a la red local
    (por ejemplo, para usar una tablet o un segundo PC como pantalla)
    requiere activación explícita y un token de acceso.
-   Durante la carrera, la GUI prioriza pocos datos grandes y legibles;
    los paneles detallados se reservan para antes y después de la
    sesión. La voz sigue siendo el canal principal durante la
    conducción.
-   Un overlay sobre el juego es una opción posterior, sujeta a que el
    juego corra en ventana sin bordes. No se inyectará nada en el proceso
    del juego; se prefiere una segunda pantalla.
-   El empaquetado como app de escritorio (por ejemplo, con Tauri o
    Electron) es opcional y posterior.
-   Los mensajes del agente se generan como estructura validable
    (RF-08) y cada interfaz decide cómo presentarlos (texto en consola,
    panel en GUI, voz).
-   Una interfaz no debe poder bloquear la recepción ni el registro de
    telemetría.

### RF-13 --- Localización (futuro, posterior al MVP)

-   Idiomas mínimos: **inglés y español**, con diseño extensible a otros
    idiomas.
-   Distinguir cuatro ámbitos de idioma, configurables por separado:
    1.  Interfaz (textos, menús, mensajes de sistema).
    2.  Comunicación del agente (avisos y respuestas generadas).
    3.  Entrada del piloto (el router de intenciones debe entender
        preguntas en ambos idiomas).
    4.  Voz (reconocimiento y síntesis), cuando exista.
-   Los textos visibles para el usuario se guardan en catálogos por
    idioma con un idioma de respaldo definido; no se codifican en la
    lógica.
-   Los prompts del LLM se versionan por idioma, y el Response Validator
    comprueba que la respuesta esté en el idioma esperado.
-   Mantener un glosario de terminología de carrera por idioma (por
    ejemplo, pit/box, undercut, stint) para respuestas consistentes.
-   Formatear números, unidades y decimales solo en la capa de
    presentación. Los datos internos, eventos, logs y registros de
    sesión usan códigos e identificadores neutros al idioma, de modo que
    una sesión grabada pueda reproducirse en cualquier idioma.
-   El mismo evento debe producir mensajes equivalentes, con las mismas
    cifras, en ambos idiomas.
-   Aunque la implementación completa sea posterior, la preparación
    (sin textos codificados, claves de mensaje, `language` en el
    contexto) se aplica desde el inicio para evitar refactorizaciones
    costosas.

## 5. Requerimientos no funcionales

-   **Local-first:** la funcionalidad principal debe operar en el equipo
    del usuario o en su red local, sin depender de servicios externos.
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
    configurados y no exponer servicios locales innecesariamente. Por
    defecto solo se escucha en loopback; abrir la aplicación a la red
    local debe ser una decisión explícita. La telemetría UDP no tiene
    autenticación, por lo que se asume una red local de confianza y se
    ofrece filtro de IP de origen. Cualquier API o GUI remota futura debe
    estar desactivada por defecto.
-   **Portabilidad de despliegue:** el mismo artefacto debe funcionar en
    ambas topologías (RF-11) y en Windows y Linux (Ubuntu/Mint) en el
    PC de la aplicación. El juego objetivo es F1 25 (2026 Season Pack).
-   **Desacoplamiento de interfaz:** poder añadir o reemplazar
    interfaces (consola, GUI, voz) sin modificar el núcleo (RF-12).
-   **Internacionalización:** textos, prompts y formatos preparados para
    varios idiomas (RF-13).

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
-   Interfaz gráfica en la primera iteración (se parte de la consola).
-   Localización completa y más idiomas además de inglés y español; en
    la primera iteración solo se deja la arquitectura preparada.
-   Acceso remoto fuera de la red local (internet, VPN o nube).

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
-   La aplicación recibe y procesa telemetría tanto con el juego y la
    aplicación en el mismo PC como con la aplicación en otro PC de la
    red local, cambiando solo la configuración.
-   El estado de conexión muestra la IP de origen y la antigüedad de la
    última muestra, y ayuda a diagnosticar la ausencia de datos.
-   El núcleo funciona sin GUI, y la consola cubre estado, avisos y
    preguntas del piloto.
-   Cuando exista la localización: el mismo evento produce mensajes
    equivalentes en inglés y español con las mismas cifras.

## 9. Decisiones tomadas

El detalle del stack está en el outline técnico, sección 8.

-   **Lenguaje y concurrencia:** Python 3.12+ con un hilo dedicado de
    recepción UDP, socket bloqueante con timeout y cola acotada para el
    consumidor. A la carga medida de F1 25 (20 Hz), `asyncio` no aporta
    una ventaja que justifique cambiar esta decisión.
-   **Parser UDP:** propio, detrás de una interfaz y validado con
    capturas reales.
-   **Integración del LLM:** API HTTP compatible con OpenAI mediante un
    adaptador intercambiable.
-   **Interfaces:** consola primero; después una GUI web local conectada
    por API local (HTTP y WebSocket); empaquetado de escritorio opcional.
-   **Comunicación núcleo-interfaces:** API local con contrato versionado
    (snapshot y cambios incrementales). La apertura a la red local es
    explícita y con token.
-   **Almacenamiento de sesiones:** captura binaria cruda para replay y
    SQLite para métricas, eventos y llamadas al LLM.
-   **i18n:** catálogos `gettext` y `Babel` para formatos; prompts y
    glosarios versionados por idioma.
-   **Juego objetivo:** F1 25 (2026 Season Pack).
-   **Sistemas operativos de la aplicación:** Windows y Linux
    (Ubuntu/Mint). La integración continua ejecuta las pruebas en ambos.

## 10. Decisiones pendientes

-   Modelo local concreto y parámetros de generación (el protocolo de
    integración ya está decidido).
-   Voz: motores de reconocimiento y síntesis, y salida de audio.
-   Tipos de paquete adicionales que se procesarán; los nueve del alcance
    inicial ya están definidos en el roadmap.
-   Esquema de SQLite. El formato de captura binaria `.f1cap` v1 ya está
    definido en el código (`src/f1_strategist/capture.py`).
-   Framework del front-end de la GUI, herramienta de generación de tipos
    y si se empaqueta como app de escritorio.
-   Fórmulas iniciales de degradación y simulación de paradas.
-   Umbrales y prioridades iniciales, que deberán calibrarse mediante
    pruebas.
-   Compatibilidad futura con formatos UDP anteriores y cambios de la
    especificación de próximos Season Packs.
-   Envío dirigido (unicast) o broadcast desde el juego, y política de
    filtrado por IP de origen.
-   Ubicación recomendada del LLM (PC del juego o PC de la red local)
    según la carga medida.
-   Gestión del token de acceso a la API cuando se abra a la red local.
-   Idioma por defecto y cambio de idioma en caliente.
