# Informe comparativo: fin de semana real en Suzuka

## Alcance

Informe para el criterio comparativo del
[Anexo B](../docs/anexo_b_perfil_del_fin_de_semana.md#10-criterio-de-salida).
No es una prueba de aprobación ni demuestra por sí solo el cierre de la fase.
Se analizó la captura local `captures\session-20261110-125520.f1cap` sin
modificarla, en la rama `phase-3.5-weekend-data`, commit `5bcf5f6`.

El usuario indicó que es **modo carrera, temporada 2026**, con programas de
práctica seguidos de vueltas adicionales, clasificación y carrera; utilizó
los valores predeterminados. Las tandas de práctica fueron de entre 4 y 6
vueltas, más cortas que las 8 a 10 sugeridas en UB1. No se conoce qué
programas concretos se ejecutaron ni en qué vueltas finalizaron.

- Archivo: 629.191.623 bytes, SHA-256
  `254141a0fea688d92388d6f47aeb35a84b4e1a082b7f7696d577cb322c6ae51f`.
- Inspección: 604.256 paquetes, 4.350,5 s de captura, sin cabeceras
  inválidas ni truncamiento detectado. La ingesta no informó rechazos del
  parser; sí emitió advertencias de historiales de tandas inválidos,
  incluido el coche del piloto en práctica y clasificación.
- El análisis usó una base SQLite temporal independiente y
  `MetricsConfig()` con los valores predeterminados: 4 vueltas limpias
  para una tanda representativa, sin corrección de ritmo por combustible.
  Los números de sesión y vuelta de las tablas son de esa ingesta; las
  vueltas corresponden al coche del piloto, índice 21.
- Las tres sesiones indican Suzuka y comparten identificadores no nulos
  de temporada y fin de semana. Sus identificadores de sesión son no
  nulos y distintos: práctica `2630414932`, clasificación `2630414962`
  y carrera `2630414972`. El identificador de fin de semana es
  `2630414932` y el de temporada `2630414732`. En este modo concreto
  los identificadores del juego permiten enlazarlas; no se generaliza a
  otros modos.

| Sesión | Tipo decodificado | Vueltas del piloto registradas | Compuestos observados |
| --- | --- | ---: | --- |
| Práctica | `practice_1` | 12 | C2 medio, C1 duro |
| Clasificación | `short_qualifying` | 3 | C3 blando |
| Carrera | `race` | 19 | C3 blando, C1 duro |

El número de registros no implica que todas sean vueltas limpias o
completas. Por ejemplo, la vuelta 12 de práctica quedó incompleta y la
19 de carrera tiene telemetría parcial.

## Comparación del desgaste

**Método:** regresión lineal del desgaste medio de las cuatro ruedas al
final de la vuelta frente a la edad del neumático, en puntos porcentuales
por vuelta (`%/vuelta`). Se seleccionaron vueltas cerradas y limpias
según la clasificación actual, con edad y cuatro valores de desgaste.
Para evitar la mezcla de compuestos descrita abajo, en este informe se
filtró además por el compuesto observado en cada `LapRecord`.

| Compuesto real / visual | Práctica: tasa y vueltas usadas | Carrera: tasa y vueltas usadas | Diferencia carrera − práctica |
| --- | --- | --- | --- |
| C1 / duro | **3,475 %/vuelta**, vueltas 8, 9, 10 y 11 | **3,042 %/vuelta**, vueltas 4, 6, 7, 8, 9, 10, 14, 15, 16 y 18 | **−0,433 puntos porcentuales/vuelta** (aprox. −12,5 % respecto a práctica) |
| C2 / medio | **4,300 %/vuelta**, vueltas 2 a 6 | Sin muestra comparable: no se observó C2 en carrera | No comparable |
| C3 / blando | Sin muestra de práctica; clasificación sin vueltas limpias suficientes | Sin vueltas limpias suficientes: se registraron dos vueltas con C3 | No comparable |

Para C1, los puntos `(edad, desgaste medio %)` de práctica fueron
`(2, 10,25)`, `(3, 13,00)`, `(4, 17,00)` y `(5, 20,50)`. Los de carrera
fueron `(2, 6,00)`, `(4, 12,25)`, `(5, 15,25)`, `(6, 18,50)`,
`(7, 21,75)`, `(8, 24,00)`, `(12, 36,25)`, `(13, 39,25)`,
`(14, 42,75)` y `(16, 49,00)`. Las edades no son necesariamente
consecutivas porque se excluyen vueltas con banderas de comparabilidad.

La práctica C1 se calculó con solo cuatro vueltas limpias de una tanda;
la carrera tuvo diez en una tanda más larga. La práctica fue con cielo
despejado y temperatura media de pista cercana a 22 °C; en carrera hubo
condiciones nubladas y temperatura cercana a 23,7 °C. También hubo
programas de práctica, diferencias de combustible y tráfico. El desgaste
es una observación física, pero estas diferencias impiden atribuir la
desviación a una sola causa o considerar calibrado el prior. El informe
no extrapola la comparación a otros compuestos o circuitos.

### Advertencia: los resúmenes guardados mezclan límites de tanda y compuesto

El último `SessionHistory` válido del piloto declara C2 hasta la vuelta
5 y C1 desde la 6 en práctica. Sin embargo, los registros por vuelta
observan C2 también en la 6 y C1 desde la 7. El resumen C1 guardado
incluye por tanto la vuelta 6 C2: informa **5 vueltas limpias** y una
tasa **4,450 %/vuelta**, frente a las **4 vueltas y 3,475 %/vuelta**
del recálculo C1 por compuesto observado. El resumen C2 guardado usa
solo las vueltas 2 a 5 y produce **4,350 %/vuelta**, frente a
**4,300 %/vuelta** incluyendo la vuelta 6 C2.

La misma discrepancia aparece en carrera: `SessionHistory` sitúa el
final de la tanda C3 en la vuelta 1 y el resumen C1 comienza en la 2,
pero el registro de la vuelta 2 indica C3 y el C1 comienza en la 3.
La vuelta 2 está marcada como entrada a boxes, daño y tráfico, por lo
que no entra en la regresión C1 de carrera; aun así, contamina otros
campos del resumen como sus límites iniciales.

La causa concreta de la discrepancia entre la convención del juego y la
asignación por vueltas **no está demostrada**. El código construye
límites a partir de `SessionHistory`
([history_ingest.py](../src/f1_strategist/metrics/history_ingest.py)) y
resume por esos intervalos
([stint_summary.py](../src/f1_strategist/metrics/stint_summary.py)).
No conviene usar el resumen C1 automático como valor validado para
calibración hasta reconciliar ambas fuentes. El recálculo de la tabla es
una comparación exploratoria, no una corrección aplicada al producto.

Para cuantificar el efecto, al reconstruir después de la ingesta un
perfil con práctica y clasificación, excluyendo la carrera, el perfil
produjo C1 **4,450 %/vuelta** y C2 **4,350 %/vuelta**. Es una
reconstrucción offline con los resúmenes guardados, **no evidencia del
perfil utilizado en vivo**. En la ingesta aislada la carrera no tenía
`profile_json` registrado; con la configuración predeterminada el grabador
SQLite está desactivado. La base predeterminada existente era de una
versión anterior y no contenía esta ruta de captura; se consultó solo en
lectura y no se migró.

## Primera vuelta rápida tras la salida de boxes

La observación del usuario está respaldada por esta captura. En práctica:

| Vuelta registrada | Tiempo oficial | Compuesto | Clasificación del histórico |
| --- | ---: | --- | --- |
| 1 | 95,906 s | C2 | `first_lap`, `out_lap`, `in_lap`, `flashback` |
| 2 | 101,013 s | C2 | Sin banderas de exclusión |
| 7 | 96,099 s | C1 | `out_lap`, `in_lap`, `flashback` |
| 8 | 98,455 s | C1 | Sin banderas de exclusión |

En los paquetes del piloto, tras la salida de boxes de la segunda tanda,
el juego conserva el número **7** durante la out lap y el cruce al inicio
de la vuelta rápida. Al cruzar, la distancia pasa de aproximadamente
5.809 m a 3 m sin cambiar ese número; después se registra el tiempo
oficial de 96,099 s para la vuelta 7. El tracker interpreta una caída
de distancia superior a 50 m en una misma vuelta como `flashback`
([lap_tracker.py](../src/f1_strategist/metrics/lap_tracker.py#L365)) y
la salida de boxes marca `out_lap`. Las banderas permanecen en el mismo
registro, que luego recibe el tiempo oficial. En la práctica analizada
no apareció un evento decodificado `flashback`; eso por sí solo no
demuestra que nunca pueda haber un rebobinado sin ese evento.

Además, la regla actual marca **toda vuelta numerada 1** como
`first_lap`, sin distinguir práctica de carrera
([comparability.py](../src/f1_strategist/metrics/comparability.py#L12)).
Por tanto, eliminar solo esa regla no resolvería la vuelta 7, que también
hereda las otras banderas. Las vueltas 2 y 8 posteriores sí se
consideraron limpias en esta ingesta; no se concluye que todas las
primeras vueltas rápidas se excluyan en todos los formatos.

**Recomendación:** sí tiene sentido contar una vuelta rápida completa
tras la out lap si sus datos corresponden a esa vuelta y no tiene otros
motivos de exclusión. Hay que separar el tramo de salida del tramo
cronometrado aunque compartan número, sin clasificar automáticamente
como flashback un cruce de línea de salida. Mantener la exclusión de la
out lap real, de los rebobinados reales y de vueltas incompletas, con
tráfico, banderas o daño según la política existente. En carrera la
primera vuelta desde parrilla puede seguir requiriendo un tratamiento
distinto.

Una **vuelta limpia** tampoco equivale por sí sola a una **tanda
representativa**: el mínimo predeterminado es cuatro vueltas limpias. La
tasa de desgaste publicada en vivo exige además al menos tres muestras
válidas para la regresión
([tyres.py](../src/f1_strategist/metrics/tyres.py#L130)); permitir la
primera vuelta rápida no debería convertir una sola muestra en una
estimación fiable.

## Seguimiento recomendado

1. Reproducir el caso con paquetes reducidos y anónimos de práctica:
   salida de boxes, cruce con mismo número y primera vuelta rápida;
   incluir una segunda tanda, la primera vuelta de carrera, un flashback
   real y condiciones de exclusión independientes.
2. Reconciliar límites de `SessionHistory`, compuesto por vuelta,
   cambios de juego y marcas de boxes; verificar resúmenes, perfil y
   muestras en vivo sin mezclar juegos o sesiones.
3. Repetir la comparación tras corregir esos casos y, si es posible,
   obtener tandas más largas y anotar qué vueltas pertenecen a los
   programas. Una nueva captura real no es necesaria para demostrar la
   discrepancia ya observada.
4. Separadamente, completar los demás criterios pendientes de la fase,
   incluido el replay con perfil registrado y la validación en Linux.
