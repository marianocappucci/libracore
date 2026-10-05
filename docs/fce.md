# La FCE completa: el registro de ARCA (WSFECRED)

> **Estado: diseño propuesto (ADR-019), sin código.** Lo que sigue sale de lo **medido en ARCA de homologación** el
> 2026-10-02/05 y del WSDL del servicio. Lo que todavía no se pudo medir está marcado 🔸.

Hoy el motor **emite** la FCE por WSFE (tipos `201/206/211`, ver `facturacion-arca.md`) y su nota de crédito **parcial**
(`notas-de-credito.md`, desde v1.130.0; la total se frena desde v1.131.0). Lo que no hace es hablar con el **registro de
FCE** de ARCA, que es otro web service: **WSFECRED**. Sin él:

- no sabe si a un receptor **le corresponde** FCE, y ARCA **no lo frena** al emitir (medido: WSFE autorizó una FCE a un
  receptor que el registro da como no obligado);
- no sabe si el comprador **aceptó o rechazó** la factura, que es lo único que habilita a **anularla por completo**
  (nota con anulación `S`: sin rechazo, `10154`);
- no ve el **estado ni el saldo** que ARCA lleva de cada FCE.

## El servicio

| | |
|---|---|
| Endpoint de homologación | `https://fwshomo.afip.gob.ar/wsfecred/FECredService` (versión `WS-2.1.6`) |
| Protocolo | SOAP *document*. Namespace `http://ar.gob.afip.wsfecred/FECredService/`, **sólo en el elemento raíz** (`<fec:xxxRequest>`); los hijos van sin calificar. El orden de los campos importa (`sequence`). |
| Autenticación | WSAA con el servicio **`wsfecred`** (`arca_wsaa.autenticar(..., servicio="wsfecred")` ya lo soporta: un ticket por certificado, ambiente y servicio). Cada pedido lleva `authRequest {token, sign, cuitRepresentada}`. |
| Habilitación | El certificado tiene que tener **`wsfecred` autorizado**; si no, WSAA contesta `coe.notAuthorized` («Computador no autorizado a acceder al servicio»). En homologación se autoriza en WSASS. 🔸 En producción, por el administrador de relaciones de clave fiscal: verificar antes del primer cliente. |
| `dummy` | Con el `Body` **vacío** (su pedido no tiene partes). Con un elemento adentro, el balanceador devuelve una línea suelta `BL… 500`. |

**Operaciones** (21): consultas (`consultarComprobantes`, `consultarCtaCte`, `consultarCtasCtes`,
`consultarHistorialEstadosComprobante`, `consultarHistorialEstadosCtaCte`, `consultarMontoObligadoRecepcion`, catálogos);
acciones (`aceptarFECred`, `rechazarFECred`, `rechazarNotaDC`, `informarCancelacionTotalFECred`,
`modificarOpcionTransferencia`); y las del agente de depósito colectivo. **Las acciones de aceptar y rechazar son del
comprador** (reciben su cuenta corriente o el comprobante que le emitieron): quien **emite** sólo consulta.

## El modelo de ARCA

- **Cada FCE nace con una cuenta corriente** (`codCtaCte`): medido, la FCE A 1-12 de homologación quedó en la `450109`, y
  su nota de crédito 203 1-4 **cuelga de la misma cuenta** (`arrayNotasDCAsociadas`, `esAnulacion N`).
- **ARCA lleva el saldo**: `importeInicial 1210.00`, `importeTotalNotasDC -121.00`, **`saldo 1089.00`** — el mismo número
  que `saldo_acreditable` del motor después de esa nota parcial. Además `saldoAceptado` (lo que el comprador aceptó).
- **Estado del comprobante**: `PendienteRecepcion`, `Recepcionado`, `Aceptado`, `Rechazado`, `InformadaAgDpto`.
  **Estado de la cuenta**: `Modificable`, `Aceptada`, `Rechazada`, `CanceladaTotal`, `InformadaAgDpto`. Aceptación
  `Tacita` o `Expresa`. Cancelación `PAR` o `TOT`. Transferencia `SCA` o `ADC`.
- **Obligación de recepción**: `consultarMontoObligadoRecepcion(cuitConsultada, fechaEmision)` → `obligado` (`S`/`N`) y,
  si corresponde, **`montoDesde`**. `consultarObligadoRecepcion` está **deprecada** (observación `9998`).
- **Catálogos**: 6 motivos de rechazo (daño en las mercaderías; vicios o diferencias de calidad o cantidad; divergencias
  en plazos o precios; no corresponde con lo contratado; vicios formales; falta de entrega o de prestación) y 6 formas
  de cancelación (compensación, transferencia, cheque, cesión, otros medios del BCRA, locación de inmuebles).
- Un comprobante que no existe da el error `1105` en el historial.

## El diseño (ADR-019, propuesto)

1. **`libracore.arca_wsfecred`, sólo consultas.** Un cliente propio, con el mismo WSAA y la misma configuración de ARCA
   que la emisión:
   - `monto_obligado(cuit, fecha) -> (obligado: bool, monto_desde: Decimal | None)`;
   - `estado_de_fce(cuit_emisor, tipo, pto_vta, numero) -> {estado, cuenta, estado_cuenta, saldo, saldo_aceptado}`
     (`consultarComprobantes` + `consultarCtaCte`);
   - `historial(...)` para el detalle.
   No acepta ni rechaza: eso es del comprador, y nuestros productos emiten.
2. **Al emitir, avisar cuando corresponde FCE.** Con un receptor con CUIT, el alta (`facturas_router` y la API para
   los productos con modelo propio) consulta `monto_obligado`; si `obligado` y el total llega a `monto_desde`, la
   respuesta **sugiere** FCE (no la impone el motor). 🔸 Si WSFE **rechaza** una factura común a un receptor obligado
   todavía no se midió: si la rechaza, el aviso pasa a ser un 422 antes de pedir el número, como la guarda del receptor.
3. **La nota total de una FCE, sólo con el rechazo del comprador.** `validar_nota_de_credito` recibe el estado de ARCA
   por una costura (el producto no lo calcula: lo pide el motor con `estado_de_fce`). Con `Rechazado`, admite la nota
   total y `armar_nota` la manda con anulación **`S`**; con cualquier otro estado, sigue como en v1.131.0 (sólo parcial,
   por menos que el saldo). 🔸 **Falta medir** que WSFE acepte la `S` tras el rechazo: necesita un segundo certificado
   de homologación que haga de comprador.
4. **El saldo de ARCA controla, no manda.** El tope de la nota sigue siendo el `saldo_acreditable` del motor (lo que
   el producto guardó). Si el saldo de ARCA difiere, se avisa: es una nota que existe allá y no acá, o al revés.
5. **Si WSFECRED falla, no se frena la emisión.** Sin respuesta (caído, certificado sin autorizar), la emisión sigue
   como hoy y la nota total de FCE queda frenada: el lado seguro. El error dice qué habilitar.
6. **Para toda la familia.** Llega a todos los productos con el bump de pin. VentaLibra y `libracommerce` además
   necesitan **emitir** FCE desde la venta, que hoy no existe: es un paso aparte.

**Fuera de alcance:** recibir FCE de proveedores (aceptar y rechazar como comprador), el agente de depósito colectivo,
`informarCancelacionTotalFECred` (🔸 falta verificar si la informa el emisor o el comprador) y `obtenerRemitos`.

**Orden propuesto:** (1) `arca_wsfecred` con las consultas y sus tests contra respuestas grabadas de homologación →
(2) el aviso de obligación en la emisión → (3) medir el rechazo con un segundo certificado → (4) la nota total con `S`.
