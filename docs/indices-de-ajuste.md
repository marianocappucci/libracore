# Índices de ajuste: de dónde salen y cómo se miden

Las fuentes de `libracore.indices_descarga` (ADR-085), medidas el 2026-10-26 antes de escribir el cliente. Si una
fuente cambia de forma, se vuelve a medir y se actualiza este archivo **antes** que el código.

## BCRA — API de estadísticas v4

- **URL:** `https://api.bcra.gob.ar/estadisticas/v4.0/monetarias/{idVariable}?desde=AAAA-MM-DD&hasta=AAAA-MM-DD&limit=N&offset=N`.
  El listado de variables es `.../v4.0/monetarias` (1.610 variables el día de la medición).
- 🔴 **La v3 está dada de baja:** `GET .../v3.0/monetarias` contesta `410` con
  `{"status":410,"errorMessages":["Método correspondiente a la v3 ha sido deprecado."]}`.
- **Forma:** `{"status":200,"metadata":{"resultset":{"count":N,"offset":0,"limit":N}},"results":[{"idVariable":40,"detalle":[{"fecha":"2026-10-26","valor":37.09000000000}, ...]}]}`,
  del más nuevo al más viejo. El valor es un número JSON con once decimales: se lee con `parse_float=Decimal`.
- **Paginación:** con `limit=3000` devolvió de una vez los 1.030 valores del ICL entre 2024-01-01 y 2026-10-26.
- **Certificado:** `httpx` lo valida sin configuración extra (históricamente la cadena del BCRA daba problemas; hoy no).

| Índice | `idVariable` | Periodicidad | Desde | Último valor medido |
|---|---|---|---|---|
| ICL (base 30-06-2020 = 1) | 40 | diaria, también fines de semana | 2020-07-01 | 37,09 (2026-10-26) |
| CER (base 02-02-2002 = 1) | 30 | diaria | — | 860,18437604432 (2026-10-26) |
| UVA (base 31-03-2016 = 14,05) | 31 | diaria | 2016 | 2.168,29 (2026-10-26) |
| Dólar mayorista A 3500 | 5 | días hábiles: un fin de semana o feriado toma el último hábil (ADR-085, addendum) | — | 1.528,263 (2026-10-23) |
| Inflación mensual (variación % del IPC) | 27 | mensual, fechada el **último día del mes**, **un decimal** | 1943 | 1,9 (2026-09-30) |

## datos.gob.ar — API de series de tiempo

- **URL:** `https://apis.datos.gob.ar/series/api/series/?ids=148.3_INIVELNAL_DICI_M_26&start_date=...&end_date=...&format=json`.
- **Serie:** «IPC. Nivel General Nacional. Base dic 2016. Mensual.» (INDEC), fechada el **primer día del mes**.
- **Forma:** `{"data":[["2026-08-01",12276.766], ...], "count":N, "meta":[...]}`. Un mes sin dato puede venir como `null`.
- 🔴 **Demora:** el 2026-10-26 el último valor era el de **agosto** (12.276,766), cuando el BCRA ya publicaba la
  variación de septiembre. Por eso el catálogo tiene las dos series (`ipc` e `ipc_variacion`), que dan el mismo ajuste
  con datos coherentes: un contrato que necesita el último mes antes de que llegue el nivel usa la variación.
- 🔑 **Con datos reales no dan exactamente lo mismo.** La variación viene redondeada a un decimal. Medido el
  2026-10-26 sobre $500.000 entre 2025-10 y 2026-08: **$639.157,75 por niveles contra $639.340,56 por variaciones**
  (0,03 %). Cuál se usa es una cláusula del contrato: el sistema tiene que decir con cuál se calculó.

## Lo que se carga a mano

- **Casa Propia:** no se encontró una API. Se carga con `libracore.db.indices_ajuste.guardar(..., fuente="manual")`.
- **Cualquier índice**, si la fuente se cae o el contrato pacta otro valor. Una descarga no pisa lo cargado a mano:
  lo informa en `manuales_distintos`.

## Cómo se vuelve a medir

    curl -s "https://api.bcra.gob.ar/estadisticas/v4.0/monetarias/40?desde=2026-10-20&limit=5"
    curl -s "https://apis.datos.gob.ar/series/api/series/?ids=148.3_INIVELNAL_DICI_M_26&last=3&format=json"
    python -m libracore.indices_descarga icl ipc --desde 2026-10-01    # contra una base de prueba
