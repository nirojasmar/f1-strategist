---
name: f1-wave-review
description: Revisa una ola completa de tareas de un plan de fase del proyecto f1-strategist (ej. "revisa la ola 3 de la fase 5"). Verifica que todo lo del plan esté implementado, busca bugs e inconsistencias entre tareas y documenta los hallazgos en review_fase_X_ola_Y.md. Úsalo siempre que el usuario pida revisar, auditar o verificar una ola o fase, aunque no mencione el nombre del archivo.
---

# Revisar una ola

Tu trabajo es **verificar y documentar**, no corregir. No modifiques código salvo que el usuario lo pida. Sé económico: trabaja sobre diffs y tests, no releyendo todo el repo.

## 1. Alcance

1. Identifica fase X, ola Y y la lista de tareas de esa ola (del "Mapa de tareas" / "Olas de trabajo" del plan). Lee solo esas secciones y las de convenciones/reglas.
2. Lee `AGENTS.md` para entender las convenciones y reglas aplicables.
3. Obtén lo implementado: `git log --oneline` y `git diff <base>...HEAD --stat`; luego diff por tarea/archivo. Lee archivos completos solo si el diff no basta:
  - Identifica explícitamente el commit base de la ola.
  - Utiliza la rama objetivo del PR o el commit registrado al comenzar la ola.
  - Si existen varios PRs, asegúrate de incluir todos los cambios correspondientes a la ola.
  - Nunca asumas un commit base sin verificarlo.
  - Si no puedes determinarlo, informa el bloqueo.

## 2. Verificación automática primero

Ejecuta y registra el resultado:

```
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Un fallo aquí es hallazgo, con salida resumida (no pegues logs enteros).

## 3. Checklist por tarea

Para cada tarea de la ola:

- **Completitud:** cada punto de **Hacer** existe (archivo, función, tipo, config, CLI, línea en registry).
- **Aceptación:** cada criterio se cumple; indica evidencia (test o línea) o ❌.
- **Pruebas:** existen las listadas; cubren casos límite del plan (`None`, umbrales, reinicio de sesión, orden de llegada, datos obsoletos); no son tautológicas.
- **Límites:** solo toca archivos propios; respeta **Fuera de alcance** (sin lógica adelantada); no cambió APIs públicas sin acuerdo.
- **Reglas del repo:** dataclasses `frozen=True, slots=True`; `from __future__ import annotations`; cálculos puros (sin `time`/E/S/hilos); sin texto visible hardcodeado (claves de mensaje); dato faltante = `None` + calidad, nunca 0; config validada con `ConfigError`.

## 4. Revisión transversal (lo que se escapa por tarea)

- Contratos entre tareas: nombres, tipos y unidades coinciden entre productor y consumidor.
- Integración: registry, config/TOML de ejemplo, `SessionPort`, CLI y stubs coherentes entre sí.
- Conflictos de merge mal resueltos, duplicados, código muerto, imports sin usar.
- Rendimiento/concurrencia donde el plan fija presupuesto o un solo escritor.
- Aislamiento de errores: un fallo en un módulo no derriba el resto.

## 5. Búsqueda de bugs

Céntrate en riesgos reales, no en estilo: off-by-one en vueltas, división por cero, regresión con pocas muestras, mutabilidad compartida, reinicio de sesión/flashback, deduplicación y cooldown, histéresis invertida, comparaciones de float, serialización no estable. Si sospechas un bug, intenta confirmarlo con un test o escenario mínimo antes de reportarlo; si no puedes, márcalo como "sospecha". Busca estos riesgos cuando sean aplicables al componente revisado. No reportes problemas hipotéticos o patrones que no estén relacionados con los cambios.

## 6. Informe: `review_fase_X_ola_Y.md`

Guárdalo en `plans/reviews/` (o donde indique el usuario). Plantilla:

```
# Revisión fase X — ola Y
**Fecha / commit / rama:**
## Resumen
Veredicto: ✅ aprobada | ⚠️ con observaciones | ❌ requiere cambios (2-3 líneas)
## Verificación automática
| Comando | Resultado |
## Estado por tarea
| ID | Implementada | Aceptación | Pruebas | Notas |
## Hallazgos
### [Severidad] H-01 — título corto
- Tarea(s): · Archivo:línea
- Descripción y evidencia (repro/test):
- Impacto:
- Corrección sugerida:
## Divergencias respecto al plan
## Riesgos para la siguiente ola
## Pendientes / sospechas sin confirmar
```

Severidades: **Crítica** (rompe contrato o datos erróneos), **Alta** (bug probable/criterio incumplido), **Media** (caso límite o test débil), **Baja** (limpieza).

## 7. Respuesta final

Máx. ~10 líneas: veredicto, ruta del informe, conteo de hallazgos por severidad y los 1-3 más importantes. No repitas el informe en el chat.
