Respuestas de WSCPE (Carta de Porte Electrónica) para `tests/test_arca_wscpe.py`.

- **Reales**, de consultas de sólo lectura en **producción** el 2026-10-07: `cpe_inexistente.xml` (error `800`),
  `no_relacionada.xml` (el `soap:Fault` de un CUIT sin delegación) y `provincias.xml` (recortada a tres).
  Anonimizadas con los CUIT de prueba de la suite: `20111111112` el certificado, `30222222223` la transportista.
- `cpe_descargada.xml` es **real**: la consulta por CTG de una CPE confirmada (`CN`, con descarga) en **producción**
  el 2026-10-09, como transportista, tras la delegación de `wscpe`. Anonimizada: CUIT de la suite (más `30888888884`
  el entregador), CTG `10100000002`, RENSPA, planta, dominios y observaciones ficticios; el PDF es un texto. Se
  conservan los códigos de provincia y localidad, los kilos, la tarifa y lo que el WSDL no deja ver: `<retiroProductor/>`
  y `<errores/>` vacíos, sin `tarifaReferencia` y el `id` de la cabecera del sobre.
- **Armada a mano** sobre el WSDL v2.2.0 (`ConsultarCPEAutomotorResp`), con el sobre real de ARCA: `cpe_activa.xml`
  (sin descarga). No hay una CPE activa real para leer; cuando la haya, reemplazarla por la grabada (anonimizada).

- **Emisión, reales de homologación (2026-10-08)**, anonimizadas igual:
  - `ult_nro_orden.xml` (`0`), `tipos_grano.xml` y `localidades.xml` (recortadas a tres);
  - `plantas_sin_plantas.xml` (`800`) y `anular_inexistente.xml` (`1302`);
  - `autorizar_rechazo_949.xml` (productor informado sin corresponder) y `autorizar_rechazo_2008.xml` (solicitante sin SISA).
  - La autorización exitosa se arma en el test sobre `cpe_activa.xml`, hasta tener un certificado de homologación de un titular con SISA.
