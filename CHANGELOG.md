# Changelog — LibraCore

La versión real la determina el tag de Git (`vX.Y.Z`, vía `hatch-vcs` — ver
`README.md`, sección Versionado). Este archivo no la reemplaza: registra QUÉ
cambió en cada minor, para que un consumidor sepa si tiene que correr una
migración antes de actualizar el pin. Se empieza a mantener con esta entrada;
las versiones anteriores están en la historia de Git y en la bitácora del wiki
del ecosistema.

## [v1.99.0] — El vuelto en `ventas_pagos` (plan ERP de VentaLibra, F2)

### Agregado

- Migración `0010_recibido_en_ventas_pagos`: columna `ventas_pagos.recibido`,
  lo que entregó el cliente por un pago (para el vuelto). Nullable y sin
  default: `NULL` = no se registró, que es el estado de todas las filas
  anteriores y de lo que escriban los productos que no la usan. LibraCore no
  la escribe; la escribe LibraCommerce sólo cuando viene un valor.

### Para los consumidores

- **Correr la `0010`** (`libracore-migrar upgrade`, que ya corren el arranque
  de los `-dev` y `panel_admin.py actualizar`). Sin ella, nada se rompe en un
  producto que no escriba `recibido`.
- Es Alembic puro, **fuera de `init_core_schema()`**, y tiene `downgrade` real:
  el patrón de la `0002`, no el de la `0004`–`0009`.

## [v1.98.0] — Cierre diario (Fase 1: motor)

### Agregado

- `libracore.db.cierre_diario`: el cierre diario como acto registrado, por
  sucursal — `cerrar_dia()`, `preview_cierre_dia()`, `dia_cerrado()`,
  `verificar_dia_abierto()`, `listar_cierres()`, `get_cierre()`,
  `get_cierre_turno()`, `sucursal_de_caja()`. Guarda una foto (por turno, por
  caja y de la sucursal) que no se recalcula al reimprimir.
- `arqueo_de_turno()`/`arqueo_turno_en_vivo()`: el arqueo de un turno para
  imprimir AL CERRARLO, antes de que exista cualquier cierre diario — misma
  cuenta que usa la foto (`_medios_de_turno`/`_diferencia`), no una copia.
  `sucursal_id=None` es "sin sucursal" en las cuatro funciones de destino del
  módulo por igual; `listar_cierres(todas=True)` es explícito para "todas".
- `libracore.db.turnos.create_turno()` ahora resuelve la sucursal de la caja
  del turno y rechaza la apertura si esa sucursal ya cerró el día — cubre a
  todo producto que abra turnos por esta función (VentaLibra, LibraClub; sin
  efecto en los que no usan sucursales).
- `libracore.ticket_generator.generar_ticket_cierre_turno()` y
  `generar_ticket_cierre_diario()`: tickets térmicos de 80 mm (PDF),
  reutilizando las piezas públicas del módulo.
- `libracore.caja_router.build_cierre_diario_router()`: preview, cerrar,
  listar, ver un cierre y los dos tickets PDF. El ticket de turno es
  `GET /turno/{turno_id}/ticket` (por el id real del turno, no por el de una
  foto): usa la foto si ya existe, si no lo calcula en vivo, y da 409 si el
  turno sigue abierto. `autorizar_cierre` es una dependencia inyectable para
  el endpoint de cierre — el motor no decide quién puede cerrar el día, eso
  lo resuelve cada producto.
- `libracore.db.logs`: nuevo tipo `cierre_diario` en la línea de tiempo de
  `get_actividad_log()`/`PARTES_LIBRACORE`/`PARTES_CORE`.

### Migración

- Nueva revisión de Alembic **`0009_cierre_diario`**: crea `cierres_diarios`,
  `cierres_diarios_turnos` y `cierres_diarios_medios` (no tocan
  `init_core_schema()`, que sigue congelada desde la `0001`).
- 🔴 **Los consumidores tienen que correr esta migración.** El deploy de esta
  familia NO corre migraciones solo (ver `README.md`, sección Migraciones) —
  el guard nuevo de `create_turno()` tolera que la tabla todavía no exista
  (se comporta como si nunca se hubiera cerrado nada), pero el cierre diario
  en sí no funciona hasta que la `0009` corra contra la base del producto:

  ```
  LIBRACORE_REF=v1.98.0 DATABASE_URL=postgresql://user:pass@host/db \
    ./scripts/run_migrations.sh
  ```

Ver `DECISIONS.md`, ADR-011, para el detalle de las decisiones de diseño.
