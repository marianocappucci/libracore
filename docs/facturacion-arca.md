# Facturación electrónica ARCA y MercadoPago — el módulo del motor

Cómo un producto de la familia enchufa facturación electrónica y cobros por
MercadoPago, y qué decisiones ya están tomadas para que no se vuelvan a tomar
distinto en cada repo.

> Esto reemplaza a los `ARCA_MODULO_REUTILIZABLE.md` que vivían copiados en
> Contalibra y Restolibra. Aquellos describían una arquitectura que ya no existe
> —`arca_wsaa.py` y `database.py` adentro del producto— y por lo tanto mandaban a
> escribir de nuevo lo que el motor ya da hecho.
>
> Para el trámite ante ARCA —sacar el certificado, habilitar los servicios— la
> guía es [`guia-certificado-arca.md`](guia-certificado-arca.md), y también es
> una sola para toda la familia.

---

## Qué pone el motor y qué pone el producto

```
┌─ libracore ─────────────────────────────────────────────────────────┐
│                                                                     │
│  Protocolo        arca_wsaa      TRA → firma CMS → token+sign        │
│                   arca_wsfe      CAE, último autorizado, consulta    │
│                   arca_wspadron  consulta de CUIT (Alcance 13)       │
│                   mp_api         pagos, movimientos, QR de caja      │
│                                                                     │
│  Criptografía     arca_certificados   validar el par ANTES de guardar│
│                                                                     │
│  Orquestación     arca_facturacion    numerar y pedir el CAE         │
│                   mp_facturacion      un cobro de MP → una factura   │
│                   mp_sync             ingesta + cron nocturno        │
│                                                                     │
│  Pantallas        arca_router         Configuración → ARCA           │
│                   mp_config_router    Configuración → MercadoPago    │
│                   mp_bandeja_router   la bandeja de cobros           │
│                   mp_webhook          la notificación de MP          │
│                                                                     │
│  Datos            db.arca_config, db.facturas, db.mp, db.caja        │
└─────────────────────────────────────────────────────────────────────┘
                              ▲
   el producto pone:  su gate de rol, su prefijo de ruta, y las dos
                      costuras de negocio (abajo)
```

**El paquete arma el router; el producto lo monta con su dependencia de rol.** Es
el mismo criterio que `config_router` y `libraauth.build_logs_router`, y existe
porque el vocabulario de roles no es el mismo en los seis productos.

---

## Enchufarlo: lo mínimo

```python
from fastapi import Depends
from libracore.arca_router import build_arca_router
from libracore.mp_bandeja_router import build_mp_bandeja_router
from libracore.mp_config_router import build_mp_config_router
from libracore.mp_webhook import build_mp_webhook_router

app.include_router(build_arca_router(),      dependencies=[Depends(require_admin)])
app.include_router(build_mp_config_router(), dependencies=[Depends(require_admin)])
app.include_router(build_mp_bandeja_router(), dependencies=[Depends(require_admin)])

# 🔴 El webhook va SIN gate de rol: lo llama MercadoPago, no un usuario
# logueado. Lo que lo protege es la firma HMAC, no una cookie.
app.include_router(build_mp_webhook_router())
```

Y el cron, una línea:

```python
# scripts/sync_mp_auto.py
from libracore.mp_sync import main
if __name__ == "__main__":
    main()
```

### El prefijo

`build_arca_router(prefix=...)` existe porque los productos ya publicaron rutas
distintas —`/api/config/arca`, `/config/arca`, `/api/arca`— y cambiar el prefijo
rompe el frontend desplegado. La ruta se normaliza producto por producto, con su
deploy, no de prepo desde el motor.

### Las dos costuras de negocio

Lo que **no** es igual en todos entra por parámetro:

| Parámetro | Dónde | Para qué |
|---|---|---|
| `manejadores_de_referencia` | `build_mp_webhook_router` | Qué hacer con un `external_reference` conocido. Contalibra reconoce `venta-123` y lo aplica a esa venta presencial en vez de tratarlo como suscripción |
| `debe_auto_facturar` | webhook y `mp_sync` | Cuándo facturar solo. Por omisión, la bandera `auto_facturar` del cliente. Contalibra le suma su regla de *Hosting Mensual*, que es su negocio y no del motor |
| `referencias_a_omitir` | bandeja y `mp_sync` | Qué cobros no traer a la bandeja porque el producto ya los maneja por otro lado |

---

## Los cuatro caminos por los que un pago de MP termina en una factura

1. El **webhook**, cuando el cliente resuelto tiene `auto_facturar`.
2. El botón *Facturar* sobre un pago pendiente de la bandeja.
3. El botón *Facturar* sobre una transferencia entrante.
4. El **cron nocturno**, que emite la mayoría y corre sin nadie mirando.

> 🔴 **Los cuatro resuelven el cliente con `db.mp.resolver_cliente_pago` y ninguno
> por su cuenta.** Esa regla se rompió una vez: cuando se agregaron los alias de
> facturación el 2026-07-13 se tocaron los tres caminos visibles desde `web/` y el
> cron quedó afuera. Facturó dos comprobantes al CUIT equivocado tres semanas
> después (RIPEHO 2026-07-10, VISCO 2026-08-03).
>
> **Al tocar la resolución de cliente, la lista de archivos es ésta, no la de los
> routers.** Hoy los cuatro caminos entran por `mp_facturacion` y `mp_sync`, así
> que el pozo está tapado — pero la regla sigue valiendo.

### Por qué el alias no es un lujo

El match directo **no es un empate: elige el cliente más nuevo.**
`get_client_by_email` ordena `activo DESC, id DESC`, y el de id más alto suele ser
el placeholder que crea el fallback de `generar_factura_mp` cuando un pago no
matchea: razón social = el email, sin CUIT, "Consumidor Final". El sistema fabrica
el duplicado que después envenena su propio match.

---

## Reglas del protocolo que ya se pagaron caro

### La firma del TRA

`openssl smime` con parámetros exactos: el **certificado primero** y la clave
segunda, `-outform DER`, `-nodetach` (contenido embebido) y `-md sha1` — SHA1, no
SHA256. Los cuatro los exige ARCA.

### El SSL de los servidores de ARCA

Usan parámetros DH legacy: sin `ctx.set_ciphers("ALL:@SECLEVEL=0")` el handshake
falla y el error no habla de eso. Ya está en `arca_wsfe`.

### El número sale de ARCA, no de la base

`FECompUltimoAutorizado + 1`. Un `MAX(numero)` local se desfasa en cuanto se
emite un comprobante desde otro sistema con el mismo punto de venta.

> ⚠️ Y aun así, **releé la factura por id después de crearla**.
> `db.facturas.create_factura` reintenta con otro número si choca contra
> `idx_facturas_numero_unico`, así que el número que pasaste puede no ser el que
> quedó. Nombrar el pedido en vez del emitido deja el movimiento de caja y el mail
> apuntando a un comprobante que no existe.

### Factura C: IVA siempre en cero

Para los tipos 11, 12 y 13: `ImpNeto = ImpTotal`, `ImpIVA = 0`, y **sin** el
bloque `<Iva>` de alícuotas. No es una simplificación: ARCA rechaza el
comprobante si se manda.

### Concepto 2 o 3 exige fechas de servicio

`FchServDesde`, `FchServHasta` y `FchVtoPago` son obligatorias cuando el concepto
es Servicios o Ambos.

### Notas de crédito y débito

El bloque `CbtesAsoc` con tipo, punto de venta y número del comprobante original
es obligatorio.

### La alícuota que no está en la tabla cae al 21%

`arca_wsfe._iva_id()` mapea `{0: 3, 10.5: 4, 21: 5, 27: 6}` y **cae al 21 ante un
porcentaje que no conoce**, sin avisar. Si un producto permite alícuotas libres,
el que valida es el producto.

### El tag de la consulta

`FECompConsultar` lleva `<FeCompConsReq>`, no `<FeConsReq>`.

---

## Los dos filtros de MercadoPago que NO hay que agregar

> 🔴 **No filtrar la bandeja por `operation_type == account_fund` ni por
> `payer.email == el propio`.**
>
> Contalibra tuvo esos dos filtros nueve días (2026-07-05 → 2026-07-14) para
> descartar auto-fondeos con tarjeta propia. Una transferencia **real** de un
> cliente quedó invisible: MercadoPago marca `account_fund` con el email propio a
> *cualquier* movimiento que no sea un pago clásico de un tercero, transferencias
> incluidas. Decisión explícita del humano: entra todo, y lo que resulte ser plata
> propia se descarta a mano.

Los cortes que sí están y son seguros: `collector_id` distinto del propio (es el
cobro de otra cuenta), importe cero o negativo, y lo ya registrado.

---

## El webhook

Cuatro reglas, y las cuatro tienen su test:

1. **La firma es lo único que separa una notificación real de una inventada.** Con
   `mp_webhook_secret` cargado, una firma que no valida es 400. La plantilla es
   exactamente `id:…;request-id:…;ts:…` y se compara con `compare_digest`.
2. **El estado se le pregunta a MercadoPago.** El payload sólo aporta el id; el
   importe, el pagador y el estado salen de consultar la API. Es la mitigación de
   que el secret sea opcional.
3. **Contesta 200 casi siempre.** MercadoPago reintenta ante cualquier código que
   no sea 2xx: devolver 500 por un problema propio convierte un error en una
   tormenta de reintentos. Las excepciones son el JSON ilegible y la firma
   inválida, donde el reintento tampoco serviría.
4. **Idempotencia por `payment_id`.** MercadoPago manda la misma notificación
   varias veces.

---

## Ambientes, dependencias y arranque

- `ENV=development` hace que `arca_facturacion` numere local y estampe un **CAE
  simulado**. Sirve para que la pantalla de dev se parezca a la de producción; no
  emite nada.
- **`openssl` tiene que estar en la imagen**: la firma del TRA lo invoca por
  `subprocess`. Un `python:3.12-slim` pelado no lo trae.
- Los certificados viven en `CERTS_DIR` (`$DATA_DIR/arca_certs`), y
  `config_manager.resolve_cert_paths` cae a los nombres estándar
  `certificado.crt` / `clave_privada.key` si el path guardado quedó obsoleto —
  por eso el router los guarda siempre con esos nombres.

---

## Checklist de un producto nuevo

```
[ ] Montar arca_router con el require_admin del producto
[ ] Montar mp_config_router y mp_bandeja_router idem
[ ] Montar mp_webhook SIN gate de rol
[ ] scripts/sync_mp_auto.py llamando a libracore.mp_sync.main
[ ] Cron nocturno apuntando a ese script
[ ] openssl en el Dockerfile
[ ] Referenciar guia-certificado-arca.md desde el README, no copiarla
[ ] Una factura de prueba con CAE en homologación
```
