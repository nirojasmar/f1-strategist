# Review de Fase 2, Ola 4: integración de métricas

| Campo | Resultado |
| --- | --- |
| Repositorio y rama | `nirojasmar/f1-strategist`, `f2-wave-4-integration` |
| Commit revisado | `a8af6b52f2e374edc002f6f60707bd67493ba65b`, coincidente con la rama remota el 2026-11-04 |
| Base de la revisión | `fe2fd8e`, Ola 3 fusionada |
| Alcance | E1 motor, E2 sesión, E3 consola, E4 exportación y CLI; dependencias previas cuando afectan la integración |
| Veredicto | **Cambios solicitados.** La suite pasa, pero hay riesgo de pérdida de la captura y resultados incorrectos o engañosamente fiables en la integración. |

## Contexto y validación

Se contrastaron los cambios con [E1--E4 del plan](plans/plan_fase_2_metricas_e_historico.md#L481), [RF-03](docs/requerimientos_ingeniero_carrera_f1_25.md#L104), la [arquitectura de métricas](docs/outline_tecnico_ingeniero_carrera_f1_25.md#L231), el [roadmap](docs/roadmap_ingeniero_carrera_f1_25.md#L129) y [progress.md](progress.md#L135). El plan consultado figura como propuesta pendiente de aprobación; se usa como referencia, sin asumir que fue aprobado.

| Comprobación | Resultado local |
| --- | --- |
| `uv run pytest -q` | 391 pruebas pasaron |
| `uv run ruff check .` | Sin errores |
| `uv run ruff format --check .` | 76 archivos con formato correcto |
| `uv run pyright` | 0 errores y 0 avisos |
| `git diff --check fe2fd8e..HEAD` | Sin errores |
| Sondas adicionales | Carreras sintéticas, paquetes de fixture, fin de captura, aislamiento de motores, calidad y exportación CLI |
| Rendimiento sintético | 22 coches, 5 vueltas, 633 paquetes: media de `MetricsEngine.apply` de aproximadamente 0,094 ms en esta máquina; no sustituye otras mediciones |
| CI remota y captura real | No verificadas |

La integración no se considera validación de F3: la calibración y las tolerancias con captura real siguen pendientes en la Ola 5. Las sondas de archivos usaron capturas temporales, no las capturas del usuario.

## Resumen

**Alta:** pérdida de datos o resultado estratégico incorrecto presentado como válido. **Media:** ausencia de diagnóstico o incumplimiento de un contrato de exportación. **Baja:** incompatibilidad de formato documentado.

| ID | Severidad | Área | Hallazgo |
| --- | --- | --- | --- |
| O4-1 | Alta | E4 | `--out` puede sobrescribir la captura de entrada |
| O4-2 | Alta | E1, E4 | La exportación puede publicar métricas anteriores al último paquete procesado |
| O4-3 | Alta | E1, histórico | La vuelta final oficial sigue excluida del ritmo; defecto heredado expuesto por la integración |
| O4-4 | Alta | E1 | Sin piloto identificado se calculan métricas como si el piloto fuera el coche 0 |
| O4-5 | Alta | E1, D5 | Motores independientes comparten el módulo mutable de intervalos |
| O4-6 | Alta | E2, E3 | Al dejar de llegar paquetes el panel conserva calidad `ok` indefinidamente |
| O4-7 | Media | E4 | El comando oculta capturas truncadas y rechazos del parser |
| O4-8 | Media | E1, E3 | Los fallos de módulos se aíslan pero no se muestran al usuario de consola |
| O4-9 | Media | E4 | El JSON enviado a stdout no es necesariamente UTF-8 en Windows |
| O4-10 | Baja | E4 | CSV escrito a archivo usa CRLF en Windows pese a documentar LF |

## Hallazgos

### O4-1 — La salida puede destruir la captura original

- **Ubicación:** [cli.py](src/f1_strategist/cli.py#L221), en particular la escritura en [cli.py](src/f1_strategist/cli.py#L233).
- **Evidencia:** en una copia temporal ejecuté `metrics CAPTURA --out CAPTURA`. El comando devolvió 0 y reemplazó la cabecera binaria `F1CAP` por texto JSON.
- **Impacto:** una elección accidental del mismo archivo, incluidos alias que apunten al mismo destino, destruye la captura que se pretendía analizar.
- **Corrección y prueba:** rechazar que salida y entrada identifiquen el mismo archivo antes de escribir; contemplar rutas relativas, absolutas y enlaces existentes según la plataforma. Verificar que la captura queda intacta y que se informa el error. Considerar además la protección del archivo de configuración si se permite indicar `--config` y `--out` simultáneamente.

### O4-2 — El último histórico procesado puede no reflejarse en el snapshot exportado

- **Ubicación:** [engine.py](src/f1_strategist/metrics/engine.py#L108), [engine.py](src/f1_strategist/metrics/engine.py#L112) y [export.py](src/f1_strategist/metrics/export.py#L102).
- **Evidencia:** la ingesta de `SessionHistory` modifica el histórico antes de evaluar la cadencia, pero no fuerza una publicación. En una sonda, un paquete oficial llegó 0,1 s después de la publicación anterior y cambió una vuelta de 90,0 a 76,543 s. El histórico quedó con 76,543 s, mientras `pace.best_lap_s` siguió en 90,0 s y el instante del snapshot no avanzó.
- **Impacto:** al terminar el archivo, JSON puede contener un resultado desactualizado aunque el runner haya procesado los datos oficiales. El histórico CSV/JSONL y el snapshot de la misma captura pueden discrepar.
- **Corrección y prueba:** establecer una finalización offline que publique con el último estado e histórico procesados, sin alterar indebidamente la política de publicación en vivo. Probar un `SessionHistory` final dentro del intervalo de publicación y comparar tiempo oficial, snapshot JSON e histórico.

### O4-3 — La vuelta final oficial no llega a las métricas de ritmo

- **Origen:** problema **heredado** de la base, no introducido por E1; la integración lo hace visible en el snapshot final y en E4.
- **Ubicación:** [lap_tracker.py](src/f1_strategist/metrics/lap_tracker.py#L565), [history_ingest.py](src/f1_strategist/metrics/history_ingest.py#L14) y [pace.py](src/f1_strategist/metrics/pace.py#L90).
- **Evidencia:** al terminar una carrera sintética de cinco vueltas, con las cuatro primeras de 90 s y la última de 70 s, el registro final tenía `lap_time_s=70.0`, fuente `session_history` y bandera `incomplete`. Sin embargo, `pace.last_lap_s` y `pace.best_lap_s` eran `90.0/ok`, porque ritmo excluye `incomplete`. La ingesta oficial actualiza el tiempo, pero no resuelve esa bandera.
- **Impacto:** incluso con el tiempo oficial presente y una publicación final, el resultado de ritmo puede omitir la última vuelta y la mejor vuelta de la carrera.
- **Corrección y prueba:** distinguir una vuelta realmente incompleta de una vuelta cuyo tiempo completo fue confirmado por la fuente oficial, sin eliminar indebidamente otras advertencias de comparabilidad. Añadir una carrera con mejor vuelta final y pruebas de retirada y cierre realmente parcial. Mantener la validación del orden real de paquetes en F3.

### O4-4 — Piloto desconocido se sustituye por el coche 0

- **Ubicación:** [engine.py](src/f1_strategist/metrics/engine.py#L125) y [engine.py](src/f1_strategist/metrics/engine.py#L155).
- **Evidencia:** `_player_car_index` devuelve 0 si el índice es `None` o está fuera de rango. Con `player_car_index=None` y datos del coche 0, la sonda publicó su compuesto `soft` y su intervalo delantero de `1,25 s` como métricas del piloto.
- **Impacto:** en telemetría sin piloto identificado, los consumidores pueden atribuir combustible, neumáticos e intervalos de otro coche al usuario. Va contra la regla de no inventar valores ausentes.
- **Corrección y prueba:** representar explícitamente la ausencia de piloto; publicar las métricas que dependen de él como no disponibles, sin impedir las métricas puramente de sesión. Probar cabecera con índice inválido y la transición posterior a un piloto identificado distinto de 0.

### O4-5 — Dos motores comparten estado mutable de intervalos

- **Ubicación:** [engine.py](src/f1_strategist/metrics/engine.py#L77), [registry.py](src/f1_strategist/metrics/registry.py#L16) y [gap.py](src/f1_strategist/metrics/gap.py#L302).
- **Evidencia:** el registro contiene instancias; `tuple(METRIC_MODULES)` reutiliza esas mismas instancias, incluida `GapMetrics` y su tracker mutable. En la sonda, dos muestras del primer motor y la primera muestra de un segundo motor con el mismo UID y coche produjeron en el segundo una tendencia de `0.0 s/s`, calidad `low`, con **tres** muestras. Se confirmó que ambos motores referenciaban el mismo módulo.
- **Impacto:** una sesión en vivo y una reproducción offline, o dos motores independientes, pueden contaminar sus tendencias. Cambiar de UID en uno también reinicia la serie del otro.
- **Corrección y prueba:** crear módulos con estado por instancia de motor o dar al motor la propiedad explícita del tracker; evitar compartirlos a través del registro global. Probar dos motores intercalados con el mismo UID y con UID distintos.

### O4-6 — La calidad del panel no envejece cuando se detiene la telemetría

- **Ubicación:** [session.py](src/f1_strategist/session.py#L85), [console.py](src/f1_strategist/console.py#L102) y [console_messages.py](src/f1_strategist/console_messages.py#L108).
- **Evidencia:** el motor solo recalcula al procesar paquetes y el panel muestra la calidad almacenada sin considerar el tiempo actual. Con una carrera en curso, 100 s después de la última muestra, `lap_data.is_stale(now)` era `True`, pero el panel seguía mostrando `Vueltas restantes 4.8 vueltas ok`.
- **Impacto:** ante pérdida de conexión o pausa, la consola puede seguir presentando como frescas cifras antiguas. El aviso general de conexión no corrige el marcador de calidad de cada métrica.
- **Corrección y prueba:** hacer que el consumidor pueda determinar la antigüedad y calidad efectiva de las fuentes al presentar el snapshot, o disponer de una política de actualización segura sin paquetes; no marcar indiscriminadamente una métrica histórica válida. Probar el panel antes y después de superar los umbrales, sin entregar nuevos paquetes.

### O4-7 — El comando ignora truncamiento y rechazos al exportar

- **Ubicación:** [offline.py](src/f1_strategist/offline.py#L30), [export.py](src/f1_strategist/metrics/export.py#L102) y [cli.py](src/f1_strategist/cli.py#L227).
- **Evidencia:** `OfflineResult` registra `truncated` y `parser_rejections`, pero la CLI no los consulta. Una captura temporal con un paquete rechazado y un registro final incompleto devolvió código 0, ningún aviso en stderr y un JSON con `metrics: {}`.
- **Impacto:** una exportación parcial o vacía parece completa y utilizable. Es especialmente peligroso para comparar métricas con el juego o usar el JSON como entrada de otra herramienta.
- **Corrección y prueba:** comunicar explícitamente truncamiento y rechazos, preservando stdout como JSON/CSV/JSONL válido; definir y documentar cuándo corresponde fallo o advertencia. Probar captura truncada, rechazos parciales y captura sin ningún paquete válido.

### O4-8 — Los errores aislados de módulos no llegan a la consola

- **Ubicación:** [engine.py](src/f1_strategist/metrics/engine.py#L133) y [console_messages.py](src/f1_strategist/console_messages.py#L108).
- **Evidencia:** un módulo que lanza `RuntimeError` produce `broken.error` con notas `metric_module_failed` y `RuntimeError`, pero no aumenta `consumer_errors` ni genera un diagnóstico visible. En la sonda, el panel no mostró el nombre del módulo ni un error; las claves esperadas aparecen como `—`.
- **Impacto:** el usuario no puede distinguir un módulo roto de datos aún no disponibles. El aislamiento evita detener la captura, pero oculta una avería persistente del cálculo.
- **Corrección y prueba:** mostrar o registrar un diagnóstico identificable por módulo sin detener los demás, evitando inundar la consola en cada publicación. Probar un módulo real fallido y otro funcional; verificar diagnóstico, continuidad del consumidor y recuperación.

### O4-9 — La salida estándar JSON puede no ser UTF-8

- **Ubicación:** [export.py](src/f1_strategist/metrics/export.py#L82) y [cli.py](src/f1_strategist/cli.py#L229).
- **Evidencia:** el serializador usa `ensure_ascii=False` y escribe con la codificación de `sys.stdout`. En esta ejecución de Windows, la salida redirigida del subproceso usó `cp1252`; una captura con el fixture de sesión produjo JSON con `°C` que **no** se pudo decodificar como UTF-8, aunque el comando devolvió 0.
- **Impacto:** herramientas que consumen JSON UTF-8 pueden rechazar la salida, aun cuando `--out` sí especifica UTF-8.
- **Corrección y prueba:** garantizar una representación compatible con UTF-8 en stdout sin introducir mensajes adicionales en el flujo estructurado. Probar un subproceso Windows con codificación de consola no UTF-8 y decodificar los bytes como UTF-8.

### O4-10 — El CSV en archivo no respeta el fin de línea documentado

- **Ubicación:** [export.py](src/f1_strategist/metrics/export.py#L123), [cli.py](src/f1_strategist/cli.py#L233) y [exportacion_metricas.md](docs/exportacion_metricas.md#L67).
- **Evidencia:** `_render_csv` genera `\n`, pero `Path.write_text` aplica traducción de fin de línea de Windows. Al exportar un CSV vacío a un archivo temporal se observó una cabecera terminada en `\r\n`; la documentación promete `\n`.
- **Impacto:** el archivo no cumple el contrato documentado ni es estable por bytes entre plataformas.
- **Corrección y prueba:** escribir con una política de salto de línea explícita y comprobar los bytes del archivo real, no solo el string devuelto por `render_metrics_export`.

## Cobertura y recomendación

- Las pruebas actuales validan la publicación periódica, el aislamiento de una excepción, la interfaz de sesión, el panel y los formatos nominales. Las sondas anteriores exponen combinaciones que esas pruebas no comprueban.
- E2 exige preservar el comportamiento «con métricas desactivadas». La configuración y los tests revisados no muestran un mecanismo para desactivar el motor; ese criterio no quedó verificado. `--no-capture` desactiva la grabación, no las métricas.
- La prueba de presupuesto de 1 ms se ejecuta en la suite normal con un umbral de reloj absoluto. El plan pide que el benchmark esté marcado para no ser frágil en CI; conviene separarlo de las pruebas funcionales y conservar una medición representativa de 22 coches.
- **Recomendación:** corregir O4-1 antes de recomendar el comando de exportación y resolver O4-2--O4-6 antes de aceptar la integración. Corregir también los diagnósticos y contratos de salida restantes; añadir las regresiones descritas y volver a ejecutar suite, Ruff y Pyright. Mantener F3 como validación independiente con captura real.
