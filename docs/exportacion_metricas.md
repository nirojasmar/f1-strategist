# Exportación de métricas e histórico

El comando `f1-strategist metrics` reproduce una captura de forma offline,
calcula las métricas con el mismo motor que usa la sesión en vivo y
exporta el resultado. No necesita conexión de red ni el juego en
ejecución.

## Uso

``` console
f1-strategist metrics CAPTURA [--out ARCHIVO] [--format json|jsonl|csv] [--config TOML]
```

- `CAPTURA`: archivo `.f1cap` o `.f1cap.gz`.
- `--out`: archivo de salida. Si se omite, el resultado se escribe en la
  salida estándar.
- `--format`: `json` (por defecto), `jsonl` o `csv`.
- `--config`: archivo TOML de configuración, incluidos los parámetros de
  `MetricsConfig`.

Los formatos `jsonl` y `csv` contienen una fila por coche y vuelta del
histórico. El formato `json` contiene la instantánea de métricas.

## Esquema JSON de métricas

El documento JSON incluye `schema_version: 1` para permitir cambios
futuros sin romper a los consumidores. Ejemplo:

``` json
{
  "schema_version": 1,
  "session_uid": 61733,
  "computed_at_monotonic": 270.759,
  "metrics": {
    "fuel.margin_laps_at_finish": {
      "value": 1.5,
      "unit": "lap",
      "quality": "ok",
      "window": { "kind": "laps", "size": 5 },
      "sample_count": 12,
      "computed_at_monotonic": 270.5,
      "sources": ["car_status", "lap_data"],
      "notes": []
    }
  }
}
```

Las claves de `metrics` están ordenadas alfabéticamente. Cada métrica
contiene:

- `value`: valor calculado o `null` si no está disponible.
- `unit`: unidad del valor.
- `quality`: `ok`, `low`, `stale`, `insufficient` o `unavailable`.
- `window`: `kind` y, cuando aplica, `size`.
- `sample_count`: número de muestras usadas.
- `computed_at_monotonic`: instante de cálculo.
- `sources`: paquetes o fuentes utilizadas.
- `notes`: claves de notas o motivos de indisponibilidad.

## Histórico en JSON Lines y CSV

Cada fila representa una vuelta de un coche. Las columnas son las del
modelo `LapRecord`, en este orden:

`car_index`, `lap_number`, `lap_time_s`, `lap_time_source`, `sector1_s`,
`sector2_s`, `sector3_s`, `valid`, `compound`, `tyre_age_start`,
`tyre_age_end`, `fuel_start_kg`, `fuel_end_kg`, `tyre_wear_start_pct`,
`tyre_wear_end_pct`, `position_start`, `position_end`, `gap_ahead_start_s`,
`gap_ahead_end_s`, `ers_harvested_j`, `ers_deployed_j`, `pit_entry`,
`pit_exit`, `pit_lane_time_s`, `pit_stop_time_s`, `track_status_seen`,
`weather_seen`, `track_temperature_c`, `traffic_fraction`,
`damage_max_delta_pct`, `flags`, `closed`, `started_at_monotonic`,
`ended_at_monotonic`.

En JSON Lines cada línea es un objeto JSON compacto. En CSV:

- La primera línea contiene siempre la cabecera.
- Los valores ausentes se escriben como campo vacío.
- Los booleanos se escriben como `true` o `false`.
- Las listas y conjuntos se serializan como JSON compacto, por ejemplo
  `["green","safety_car"]`.
- Las filas se ordenan por `car_index` y `lap_number`.
- Se usa `\n` como fin de línea.

## Estabilidad

El esquema JSON está versionado con `schema_version`. Las claves de
métricas y las columnas del histórico son parte del contrato de
exportación: cualquier cambio incompatible debe incrementar la versión
y documentarse aquí.
