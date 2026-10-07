Respuestas de WSCPE (Carta de Porte Electrónica) para `tests/test_arca_wscpe.py`.

- **Reales**, de consultas de sólo lectura en **producción** el 2026-10-07: `cpe_inexistente.xml` (error `800`),
  `no_relacionada.xml` (el `soap:Fault` de un CUIT sin delegación) y `provincias.xml` (recortada a tres).
  Anonimizadas con los CUIT de prueba de la suite: `20111111112` el certificado, `30222222223` la transportista.
- **Armadas a mano** sobre el WSDL v2.2.0 (`ConsultarCPEAutomotorResp`), con el sobre real de ARCA:
  `cpe_activa.xml` (sin descarga) y `cpe_descargada.xml` (con los kilos de descarga). Todavía no hay una CPE real
  que se pueda leer; cuando la haya, reemplazarlas por la grabada (anonimizada). CUIT y dominios ficticios; el PDF
  es un texto, no un PDF.
