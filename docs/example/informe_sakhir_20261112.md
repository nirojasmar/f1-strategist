# Informe de captura real: carrera de alta degradación en Sakhir

## Alcance

Informe de seguimiento para la validación del motor de estrategia de Fase 4. No
es una prueba de aprobación ni demuestra por sí solo el cierre de S10. Se analizó
la captura local `captures\session-20261112-162235.f1cap` sin modificarla. El
usuario reportó las observaciones del HUD incluidas en este informe; no se
dispone de una grabación visual de la pantalla ni de evaluaciones guardadas que
permitan atribuirles una causa.

- Archivo original: 674.036.037 bytes, SHA-256
  `be5192c4b2a94d1d0f38f05e4f7e222651d5fdf3f087d06b0d863f53c9dbf372`.
- `inspect`: 645.771 paquetes, 4.635,0 s entre el primero y el último paquete,
  cero cabeceras inválidas y sin truncamiento.
- El fin de semana contiene tres sesiones vinculadas: práctica `practice_1`,
  clasificación `short_qualifying` y carrera `race`. Todas indican Sakhir; la
  carrera tiene 20 vueltas y una longitud de 5.408 m.
- Se generaron dos recortes independientes y anonimizados para el
  [manifiesto de capturas reales](../../tests/fixtures/real/manifest.toml):
  [práctica](../../tests/fixtures/real/sakhir-practice-12-laps.f1cap.gz) y
  [carrera](../../tests/fixtures/real/sakhir-race-20-laps-high-wear.f1cap.gz).
  Se conservaron paquetes de sesión, vueltas, eventos, participantes, estado,
  daños, historial de tandas, juegos y posiciones; estado e historial se
  diezmaron a uno de cada dos paquetes. El recorte de carrera conserva los
  eventos `SCAR`.
- Las expectativas de ambos escenarios se calcularon de nuevo desde los
  recortes: 22 coches, juego 1.26; práctica, mejor vuelta 90,291 s; carrera,
  mejor vuelta 91,699 s y 22 paradas contabilizadas en la clasificación final.

## Degradación y parada del piloto

La telemetría de carrera confirma un stint inicial con C3 blando y una parada en
la vuelta 9 por C1 duro. El desgaste medio de las cuatro ruedas al cierre de la
vuelta 8 fue **46,25 %**. La pérdida de posiciones inmediatamente tras la parada
es visible en los datos: el piloto pasó de P1 a P13 y volvió a P1 en la vuelta
12.

| Vuelta | Compuesto visual | Edad reportada | Desgaste medio al cierre | Posición |
| ---: | --- | ---: | ---: | ---: |
| 1 | Blando (C3) | 0 | 6,25 % | 1 |
| 4 | Blando (C3) | 3 | 23,50 % | 1 |
| 8 | Blando (C3) | 7 | **46,25 %** | 1 |
| 9 | Duro (C1) | 0 | 2,00 % | 13 |
| 12 | Duro (C1) | 3 | 11,50 % | 1 |
| 20 | Duro (C1) | 12 | 31,75 % | 1 |

El usuario reportó que el juego mostraba estas dos estrategias de carrera:

1. Blando hasta la vuelta 10 y medio hasta el final.
2. Blando hasta la vuelta 9 y duro hasta el final.

La telemetría respalda que se ejecutó una parada en la vuelta 9 con duro; no
demuestra cuál era la estrategia óptima ni que parar compensara frente a
mantener. Tampoco sustituye la carrera pendiente en la que mantener sea
claramente mejor.

## Safety car

La carrera tuvo un safety car completo observable tanto en el estado de sesión
como en cuatro eventos `SCAR`: despliegue durante la vuelta 17, retorno y
reanudación en la vuelta 18. El estado pasó a `full_safety_car`; no fue solo una
observación visual del piloto.

El safety car está fuera del alcance del MVP de esta fase. Esta captura deja
constancia de que el modelado posterior del motor debe tratarse como prioridad:
el estado cambia el coste de parar y las ventanas/posiciones de reincorporación.

## Observaciones reportadas del HUD

Lo siguiente es observación del usuario durante la carrera, no una conclusión
derivada de la captura de telemetría:

- Durante la mayor parte de la carrera no apareció recomendación por «datos
  insuficientes».
- «Mantener» apareció durante unas cuatro vueltas desde aproximadamente la
  vuelta 10; después volvió a aparecer que faltaban datos.
- El dato de reincorporación desapareció durante aproximadamente dos vueltas,
  poco después de que todos pararan.
- Compuesto, juego, vuelta de parada, ganancia contra la línea base e intervalo
  de ganancia permanecieron como «—».

No está determinado si los campos «—» o los cambios entre «mantener» y «datos
insuficientes» se deben a la falta de muestras, a que la evaluación no tenía una
parada accionable, a disponibilidad temporal de entradas, o a la presentación
del HUD. La captura por sí sola no conserva la evaluación que se vio en
pantalla.

## Seguimiento recomendado

1. Reproducir la carrera con registro por vuelta de acción, calidad, motivos,
   entradas ausentes y alternativas evaluadas; investigar el cambio
   `INSUFFICIENT_DATA` → `NO_CHANGE` → `INSUFFICIENT_DATA`.
2. Precisar en el HUD la diferencia entre campo desconocido, no aplicable y
   alternativa calculada sin recomendación. Verificar por qué la reincorporación
   no se publicó durante las vueltas observadas.
3. Investigar un prior externo, obtenido de carreras reales, para estimar la
   posición de reincorporación incluso antes de la primera parada rival.
   Presentarlo como estimación con incertidumbre y refinarlo cuando las
   posiciones y huecos en vivo permitan calcularla con mayor precisión.
4. Después del MVP, priorizar el modelo de safety car en el motor y añadir
   escenarios de regresión que cubran su despliegue, permanencia y retirada.
5. Completar UE2 con una carrera real donde mantener sea claramente mejor; esta
   captura solo cubre el caso de alta degradación y una estrategia de una parada.
