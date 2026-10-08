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

### Más de un servicio de ARCA en la misma pantalla (ADR-032)

Por omisión el router sólo conoce la facturación (`wsfe`). Un producto que además
usa otro servicio —LibraCargo, con el CTG y la Carta de Porte (`wscpe`)— lo declara
al montar, y la pantalla de Configuración / ARCA pinta un bloque por servicio:

```python
app.include_router(build_arca_router(servicios=("wsfe", "wscpe")),
                   dependencies=[Depends(require_admin)])
```

| Ruta | Qué hace |
|---|---|
| `GET {prefix}/servicios` | Los servicios del producto, cada uno con `etiqueta`, `ayuda` y el estado de sus dos pares. Existe siempre; con la facturación sola lista un bloque |
| `GET {prefix}/servicios/{servicio}/estado` | El estado de ese servicio: por ambiente `tiene_certificado`, `tiene_clave`, `completo`, `vence`, `dias_para_vencer`, `vencido`, `sujeto` y `cuit_certificado` |
| `POST {prefix}/servicios/{servicio}/certificado` · `/clave` | Sube una mitad del par de **un ambiente** (`?ambiente=` obligatorio), con las mismas validaciones y la misma auditoría que la facturación |
| `DELETE {prefix}/servicios/{servicio}/credenciales` | Saca el par de un ambiente |
| `POST {prefix}/servicios/{servicio}/probar` | Se autentica por WSAA **para ese servicio**; traduce `coe.notAuthorized`, `cms.cert.untrusted` y compañía y deja el texto de ARCA al final. Si el servicio tiene `dummy` (`wscpe`), informa si está arriba |

Las credenciales de estos servicios viven en `arca_credenciales_servicio`, una fila por
`(empresa, servicio, ambiente)` (`libracore.db.arca_credenciales_servicio`,
`arca_credenciales.paths_en_disco_de_servicio`); la facturación sigue en `arca_config`
y en sus rutas de siempre. En `wscpe` el certificado puede estar a nombre de la persona
que representa a la empresa: el CUIT que se opera va en cada llamada
(`cuitRepresentada`), no sale del certificado.

### El pedido de certificado: la clave nace en el servidor (ADR-036)

Para una empresa nueva —o una renovación— el motor genera la clave y el `.csr`, y la clave
no sale nunca. Las mismas cuatro rutas, para la facturación y para cada servicio, con
`?ambiente=` **obligatorio**:

| Ruta | Qué hace |
|---|---|
| `POST {prefix}/pedido` · `POST {prefix}/servicios/{servicio}/pedido` | Cuerpo `{cuit, razon_social, alias, reemplazar}`. Genera la clave **pendiente** (aparte de la vigente, sin pisarla) y devuelve el pedido con el `.csr` en PEM. 409 si ya hay uno pendiente, salvo `reemplazar: true` |
| `GET …/pedido` | `{pendiente, alias, cuit, razon_social, sujeto, creado}`; sin pedido, `{pendiente: false}` |
| `GET …/pedido.csr` | El `.csr` como descarga (`{alias}.csr`) |
| `DELETE …/pedido` | Descarta el pedido y su clave. El par vigente no se toca |

Al subir el `.crt` por `…/certificado`: si empareja con la clave pendiente, el par queda
instalado y el pedido se borra; si empareja con la vigente, como siempre; si no empareja con
ninguna, 422. Los estados (`GET {prefix}`, `/estado`, `/servicios…`) suman `pedido` dentro del
par del ambiente **sólo cuando hay uno pendiente**. La clave pendiente vive en `CERTS_DIR`
(`pedido-{servicio}-{ambiente}-{huella}.key`, 0600) y entra en el respaldo; ninguna respuesta,
asiento de auditoría ni log la lleva. Ver `libracore.arca_pedidos`.

### Las dos costuras de negocio

Lo que **no** es igual en todos entra por parámetro:

| Parámetro | Dónde | Para qué |
|---|---|---|
| `manejadores_de_referencia` | `build_mp_webhook_router` | Qué hacer con un `external_reference` conocido. Contalibra reconoce `venta-123` y lo aplica a esa venta presencial en vez de tratarlo como suscripción |
| `debe_auto_facturar` | webhook y `mp_sync` | Cuándo facturar solo. Por omisión, la bandera `auto_facturar` del cliente. Contalibra le suma su regla de *Hosting Mensual*, que es su negocio y no del motor |
| `referencias_a_omitir` | bandeja y `mp_sync` | Qué cobros no traer a la bandeja porque el producto ya los maneja por otro lado |
| `registro` | los cuatro | De dónde salen los clientes. Ver abajo — es la costura más importante |

---

## De dónde salen los clientes: el puerto

🔑 **El módulo de MercadoPago no sabe dónde viven los clientes.** Lo recibe.

Nació extraído de Contalibra y se trajo puesta una suposición de allá —que el
registro de clientes es `libracore.db.clients`— que es cierta en dos productos y
falsa en los otros cuatro:

| Productos | De dónde sale el cliente |
|---|---|
| Contalibra, Restolibra | `libracore.db.clients` |
| Gestiolibra, MedLibra | `libragenda.Client` + una fila de extensión local |
| LibraClub, VentaLibra | su propio dominio |

⚠️ **Y eso no es falta de normalización: es la normalización correcta.** Un
producto de turnos saca el cliente de su motor de agenda, que es de donde
cuelgan `appointments` y —en MedLibra— la historia clínica. Unificar todo en
`libracore.clients` rompería seis claves foráneas y cambiaría la identidad del
cliente de `String(100)` a `INTEGER`. Analizado y decidido el 2026-08-12
(`wiki/analyses/clientes-transversal-familia-libra.md`).

Lo transversal es **el flujo**: la firma del webhook, la idempotencia, la
ingesta única, los cuatro caminos por un solo punto de resolución. Nada de eso
depende de dónde esté guardado el cliente.

```python
from libracore.registro_de_clientes import RegistroDeClientes

class RegistroDeMiProducto:
    def resolver(self, payer_email: str, payer_cuit: str) -> dict | None: ...
    def crear(self, *, nombre, email="", cuit_dni="",
              iva_condition="Consumidor Final", address="") -> dict: ...
    def buscar_muchos(self, emails: set, cuits: set) -> tuple[dict, dict]: ...

app.include_router(build_mp_bandeja_router(registro=RegistroDeMiProducto()))
app.include_router(build_mp_webhook_router(registro=RegistroDeMiProducto()))
```

**Sin `registro` se usa el de LibraCore**, que es lo que Contalibra y Restolibra
ya tenían: para ellos no cambia nada.

Un cliente es un `dict` con `id`, `name` (la única obligatoria), `cuit_dni`,
`iva_condition`, `address`, `email` y `auto_facturar`. El `id` puede ser entero
o texto — el motor no lo interpreta.

> ⚠️ **Los alias son del registro, no del motor.**
> `facturacion_alias.cliente_id` es `INTEGER`, así que la tabla de alias de
> LibraCore **no sirve** para un registro cuya identidad es texto. Un producto
> así trae su propio almacenamiento de alias, o no tiene alias. Y `resolver()`
> es un solo método a propósito: con dos —uno de alias y otro de match— habría
> dónde saltearse el alias, que es exactamente lo que se rompió una vez y costó
> dos comprobantes al CUIT equivocado.

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

**La nota de crédito de la familia es una sola, del motor:** ver [`notas-de-credito.md`](notas-de-credito.md).

### La alícuota que no está en la tabla cae al 21%

`arca_wsfe._iva_id()` mapea `{0: 3, 10.5: 4, 21: 5, 27: 6}` y **cae al 21 ante un
porcentaje que no conoce**, sin avisar. **Es un hueco del motor, no del producto:** lo que
corresponde es que el motor rechace una alícuota que no conoce en lugar de caer al 21%
(pendiente). Mientras tanto ningún producto agrega su propia validación: se arregla acá.

### El tag de la consulta

`FECompConsultar` lleva `<FeCompConsReq>`, no `<FeConsReq>`.

---

## Factura de Crédito Electrónica MiPyME (FCE)

Tipos `201/206/211` (A, B, C) y sus notas `202/203`, `207/208`, `212/213`; viven
en `libracore.tipos_comprobante`, que es **la** lista: no copiar `(1, 6, 11)` en
un listado nuevo.

**Habilitarla** es cargar el CBU (22 dígitos) y la modalidad (`SCA` o `ADC`) en
`PUT /config/arca` (`fce_cbu`, `fce_transmision`). Hasta entonces el selector no
la ofrece y `POST /api/facturas` contesta 422. Sin pantalla nueva: entra como una
opción más del selector de tipos.

Lo que ARCA exige, **medido en homologación** (2026-10-02):

| Qué | Error si falta |
|---|---|
| `FchVtoPago`, **aunque el concepto sea Productos** | 10163 |
| CUIT del receptor (no consumidor final) | 10015 |
| Opcional `2101`: CBU del emisor, 22 dígitos | |
| Opcional `27`: `SCA` o `ADC` | 10216 |
| Nota: `CbtesAsoc.CbteFch`, la fecha del asociado | 10158 |
| Nota: **sólo** el opcional `22` (`S`/`N`), nunca el 2101 ni el 27 | 10172 |

🔴 El `22 = S` (anula) sólo lo acepta ARCA si el comprador **rechazó** la factura
(10154); la nota sale siempre con `N`.

La condición del receptor es válida **por clase** (`CondicionIVAReceptorId`): una
FCE B a un inscripto da 10243.

**El registro de FCE (WSFECRED)** —obligación de recepción, aceptación y rechazo del comprador, saldo— es otro web
service; lo medido y el diseño propuesto están en `fce.md` (ADR-019).

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
