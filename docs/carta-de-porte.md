# Carta de Porte Electrónica (WSCPE) — lectura

`libracore.arca_wscpe` (ADR-034) lee de ARCA una Carta de Porte Electrónica automotor por su CTG. **Sólo lectura**:
emitir y el ciclo de vida de la CPE son otra etapa.

## Lo medido

| | |
|---|---|
| Endpoints | homologación `https://cpea-ws-qaext.afip.gob.ar/wscpe/services/soap`, producción `https://cpea-ws.afip.gob.ar/wscpe/services/soap` (en `arca_servicios.CATALOGO["wscpe"]`) |
| Protocolo | SOAP *document*. Namespace `https://serviciosjava.afip.gob.ar/wscpe/` (con `https`; el manual también muestra `http`), **sólo en la raíz** (`<wsc:ConsultarCPEAutomotorReq>`); los hijos sin calificar. `SOAPAction` entre comillas. |
| Autenticación | WSAA con el servicio `wscpe` (ticket cacheado en disco por `arca_wsaa.autenticar`). Cada pedido lleva `auth {token, sign, cuitRepresentada}`. |
| Representación | El certificado puede ser de una persona que actúa por la empresa. `cuitRepresentada` tiene que estar entre las **relaciones del ticket** (`cuits_habilitados`); si no, `soap:Fault` → `CuitNoRelacionado`. Se habilita delegando `wscpe` al alias del certificado en el Administrador de Relaciones; ARCA lo ve con el **próximo** ticket. |
| CPE inexistente | Error `800` «No existen solicitudes…», también si el CUIT representado no interviene en esa CPE → `CpeNoEncontrada`. |
| Listar «mis CPE» | No existe: sólo por CTG o por tipo + sucursal + número de orden. |

## Uso

```python
from libracore import arca_wscpe

cpe = await arca_wscpe.consultar_por_ctg("suitrans", "produccion", cuit_representada="30XXXXXXXXX", ctg=10100000001)
cpe.numero, cpe.estado_descripcion            # "00001-00072413", "Activa"
cpe.carga.peso_neto, cpe.tiene_descarga       # 29300, False → volver a consultar hasta el arribo
cpe.transporte.cuit_chofer, cpe.transporte.cuit_pagador_flete
cpe.pdf                                       # bytes, o None
cpe.respuesta_xml                             # para archivar, sin el PDF
```

El CUIT representado **no tiene valor por defecto**: el producto lo elige en cada llamada.
