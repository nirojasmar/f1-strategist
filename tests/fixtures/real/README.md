# Fixture real: Monza, 13 vueltas

- **Origen:** `captures/session-20261101-233816.f1cap`
- **SHA-256 del origen:** `D8F2F157A34B700F7951EB1621FEB656B8ED8874CEE2A73BB3688405D15F1A3C`
- **Archivo:** `monza-13-laps.f1cap.gz` (`5592587` bytes)
- **Comando:**

  ```

## Capturas del 7 y 8 de noviembre

Las dos fuentes contienen práctica, clasificación y carrera en un mismo archivo. Se aislaron por UID
las sesiones cuyo tipo era `race` antes de aplicar el recorte, para que `--laps` no seleccionara por
error las vueltas de práctica o clasificación. El recorte conserva los tipos 1, 2, 3, 4, 7, 8, 10, 11
y 12; diezma los paquetes de alta frecuencia con la misma receta que el fixture de Monza y anonimiza
UID, participantes, enlaces, tiempos y origen.

| Fixture | Origen (SHA-256) | Escenario |
| --- | --- | --- |
| `baku-13-laps-vsc-retirement.f1cap.gz` | `captures/session-20261107-214535.f1cap` (`9a07a0ee86b5be8611dfdf10134ffc86cc2eb0890cbc3b3c6e7e48a7f4ff7ac0`) | Baku, 13 vueltas; VSC, parada y DNF del piloto (U1/U3) |
| `melbourne-20-laps-finished.f1cap.gz` | `captures/session-20261108-115221.f1cap` (`11d2ade569ea2c424fd845042588f690b9dc89f138f425a2326e19259e368cb7`) | Melbourne, 20 vueltas; piloto finaliza con una parada, grabada tras ajustes de métricas |

## Suzuka, 10 de noviembre

De la captura de fin de semana `captures/session-20261110-125520.f1cap` se aislaron por UID la
práctica 1 (12 vueltas observadas) y la carrera (19 vueltas); la clasificación no forma parte de
UE1. Ambas se recortaron con `trim`, tipos de paquete y decimación como los fixtures anteriores,
y `--anonymize`. SHA-256 de la captura de origen:
`254141a0fea688d92388d6f47aeb35a84b4e1a082b7f7696d577cb322c6ae51f`.

| Fixture | Tamaño | SHA-256 | Escenario |
| --- | ---: | --- | --- |
| `suzuka-practice.f1cap.gz` | 3.919.907 bytes | `d1106b972055d0176a3f1119f5ae6c0d1c1e188100c64a7d77f6d513aabccddf` | Práctica 1, 12 vueltas, regresión H1/H2 |
| `suzuka-19-laps.f1cap.gz` | 10.057.657 bytes | `88da5c862d3860391a84c3cfa09b93b7d153063b3a689e19ccb8af5ef8e0d072` | Carrera, 19 vueltas, regresión H1/H2 y S10 |

Las expectativas reproducibles, tamaños y SHA-256 de los recortes están en `manifest.toml`.
Verificar todos los escenarios con:

```powershell
uv run f1-strategist datasets verify --manifest tests\fixtures\real\manifest.toml
```powershell
  uv run f1-strategist trim captures\session-20261101-233816.f1cap tests\fixtures\real\monza-13-laps.f1cap.gz --types 1,2,3,4,7,8,10,11,12 --decimate 2=6 --decimate 7=6 --decimate 10=6 --decimate 11=2 --decimate 12=2 --laps 1-13 --anonymize