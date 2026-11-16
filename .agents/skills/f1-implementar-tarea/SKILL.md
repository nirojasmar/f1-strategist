---
name: f1-implementar-tarea
description: Implementa UNA tarea de un plan de fase del proyecto f1-strategist (IDs como A1, B2, E1, D4). Úsalo siempre que el usuario pegue una tarea con "Objetivo / Leer primero / Hacer / Aceptación / Pruebas / Fuera de alcance", o pida implementar una tarea de plan_fase_X. Aplica aunque el mensaje sea solo el texto de la tarea, sin instrucciones adicionales.
---

# Implementar una tarea del plan

Una conversación = una tarea. Sé económico: lee solo lo necesario.

## 1. Preparación (mínima)

1. Extrae del mensaje: ID, **Hacer**, **Aceptación**, **Pruebas**, **Fuera de alcance**.
2. Lee `AGENTS.md` para entender las convenciones y reglas aplicables.
3. Después, lee SOLO lo listado en "Leer primero".
4. Después, utiliza búsquedas dirigidas (rg, grep) para
   identificar referencias, consumidores, implementaciones
   existentes y pruebas directamente relacionadas con la tarea.
5. No explores módulos ajenos ni leas documentación completa
   salvo que sea necesario para resolver una dependencia.
6. Si el plan referencia "Contratos entre tareas" o secciones de convenciones, lee esas secciones concretas, nada más.
7. Verifica las dependencias en el HEAD actual y en la rama base, si una dependencia requerida no está disponible en el código actual, detente y repórtalo. Nunca inventes contratos ni implementes silenciosamente
  tareas anteriores.

## 2. Implementación

- Toca solo los archivos que la tarea posee. Excepciones permitidas: una línea en `registry.py` o `config.example.toml` si la tarea lo indica.
- No cambies APIs públicas existentes salvo que la tarea lo autorice explícitamente.
- Respeta **Fuera de alcance** al pie de la letra: nada de lógica extra "ya que estoy".
- Estilo del repo: `from __future__ import annotations`, dataclasses `frozen=True, slots=True`, 100 columnas, identificadores en inglés, cálculos puros (sin `time`, E/S ni hilos; el instante llega como parámetro).
- Texto visible solo con claves de mensaje (`messages.message(...)`); nunca literales en la lógica.
- Dato faltante = `None` + calidad adecuada. Nunca sustituir por 0 ni inventar.
- Imita el estilo de los módulos que la tarea indica como modelo.

## 3. Pruebas

- Escribe las pruebas listadas en **Pruebas** y una por cada criterio de **Aceptación**.
- Usa `tests/scenarios.py` y `tests/fixtures/`. Nunca dependas de capturas completas.
- Incluye casos límite (vacío, `None`, límites de umbrales, reinicio de sesión).

## 4. Definición de terminado

Ejecuta y deben pasar, en este orden:

```
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Corrige solo lo que rompa tu cambio. Si falla algo ajeno a tu tarea, repórtalo sin arreglarlo.

## 5. Cierre

- NO edites `progress.md` ni docs salvo que la tarea lo pida. Deja una nota de una línea para el PR.
- Comprueba git status antes de modificar archivos.
- Nunca sobrescribas cambios preexistentes del usuario.
- Crea una rama nueva siguiendo la convención del proyecto,
  salvo que el entorno ya haya asignado una rama de trabajo.
- Haz commits únicamente cuando el usuario o el flujo de
  trabajo del repositorio autorice esta operación.
- Nunca hagas merge automáticamente.
- Respuesta final breve (máx. ~15 líneas):
  - Archivos creados/modificados.
  - Criterios de aceptación: ✅/❌ uno por uno.
  - Comandos de verificación y resultado.
  - Decisiones o suposiciones tomadas, y cualquier desviación del plan.
  - Bloqueos o dudas para el revisor.

## Reglas de ahorro

- No reimprimas archivos ni diffs completos en la respuesta.
- No hagas refactors, limpiezas ni mejoras fuera de la tarea.
- Si el plan es ambiguo, elige la opción más simple que cumpla la aceptación y anótala en "suposiciones".
