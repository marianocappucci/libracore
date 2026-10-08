# Carta de Porte Electrónica (WSCPE) — lectura

`libracore.arca_wscpe` lee de ARCA una Carta de Porte Electrónica automotor por su CTG (ADR-034) y la **emite** por
delegación (ADR-035). Desvío, contingencia y confirmación de arribo todavía no.

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

## Emitir (ADR-035)

```python
from libracore import arca_wscpe as w

solicitud = w.SolicitudCpe(
    cuit_solicitante="33XXXXXXXXX", sucursal=1,            # el titular que delegó
    origen=w.OrigenPlanta(cod_provincia=12, cod_localidad=5321, planta=77),   # o OrigenCampo(..., renspa=)
    cod_grano=23, cosecha=2526, peso_bruto=45200, peso_tara=15900,
    destino=w.DestinoSolicitud(cuit="30XXXXXXXXX", cod_provincia=12, cod_localidad=4211, planta=1234),
    cuit_destinatario="30XXXXXXXXX",
    transporte=w.TransporteSolicitud(cuit_transportista="30XXXXXXXXX", dominios=("AA123BB",),
                                     fecha_hora_partida=partida, km=310, cuit_chofer="20XXXXXXXXX",
                                     cuit_pagador_flete="30XXXXXXXXX"),
)
cpe = await w.emitir_cpe(solicitud.cuit_solicitante, token, sign, solicitud, ambiente="produccion")
cpe.nro_ctg, cpe.numero, cpe.pdf
```

| Lo medido en homologación (2026-10-08) | |
|---|---|
| Origen en campo con `esSolicitanteCampo=false` | `949`: por eso sale del origen y no se elige |
| Origen en campo, solicitante sin actividad de productor | `1015` |
| Origen en planta, solicitante sin SISA | `2008` |
| Anular una que no existe | `1302` |
| En homologación | el certificado sólo opera por su propio CUIT: para una emisión exitosa hace falta el certificado de homologación **del titular** |

🔴 Si ARCA no contesta al autorizar, `emitir_cpe` **no reintenta**: consulta por el número pedido y, si no puede saberlo, levanta `EmisionIncierta` con la sucursal y el número. Se consulta antes de volver a emitir.
