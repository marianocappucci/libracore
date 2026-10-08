# Guía: sacar el certificado de ARCA y dejar la instancia facturando

Esta guía es **una sola para toda la familia Libra**. Los productos la referencian;
no la copian. Si algo de acá cambia, cambia para todos.

Es para la persona que da de alta una instancia: qué pedirle a ARCA, en qué orden, y
qué cargar después en la pantalla de Configuración → ARCA.

> ⏱️ **Empezá con tiempo.** Entre que subís el pedido y ARCA activa el certificado
> pueden pasar horas. No es un trámite para el día que el cliente quiere emitir.

---

## Lo que vas a terminar teniendo

| Archivo | Qué es | Cuidado |
|---|---|---|
| `clave.key` | La clave privada. **Es la identidad digital del contribuyente.** | Nunca se comparte, nunca entra a git, no viaja por mail |
| `alias.crt` | El certificado que ARCA devuelve | Público, pero igual conviene tratarlo con cuidado |

Desde la pantalla de Configuración → ARCA, **la clave se genera dentro del servidor y
no sale nunca** (ver abajo). Quien ya tiene un par hecho afuera lo sube de a una mitad.
**No hace falta entrar al servidor ni copiar nada a mano.**

---

## El camino recomendado: «Generar pedido de certificado»

Para una empresa nueva, o para renovar un certificado por vencer. **Es el paso 1 de
abajo hecho por el sistema**, y la clave nunca viaja por un mail ni por un chat.

1. En **Configuración → ARCA**, en la tarjeta del ambiente (homologación o producción),
   apretá **Generar pedido de certificado**. Completá el **CUIT**, la **razón social** y un
   **alias** (sólo letras y números; el sistema propone uno).
2. Descargá el **`.csr`**. Es lo único que sale del servidor: la clave privada queda
   guardada ahí, en un lugar aparte, **sin tocar la que ya esté cargada**. La pantalla
   muestra los pasos para ARCA con el CUIT y el alias de esa empresa.
3. Hacé los pasos 2 y 3 de esta guía (subir el `.csr` a ARCA, descargar el `.crt` y
   habilitar el servicio). Mientras tanto la tarjeta dice *«Esperando el certificado de
   ARCA (pedido del dd-mm-aaaa)»*; se puede volver a descargar el `.csr` o **descartar el
   pedido** (se pierde su clave).
4. Cuando ARCA devuelve el `.crt`, **subilo en esa misma tarjeta, sin campo de clave**. El
   sistema comprueba que es pareja de la clave del pedido y, si lo es, deja el par
   instalado. Si no es pareja de ninguna clave, lo rechaza y dice por qué.
5. Apretá **Probar**.

Qué cambia según el ambiente en el paso 3:

| | Homologación | Producción |
|---|---|---|
| Dónde se sube el `.csr` | **WSASS** (*Autogestión de Certificados Homologación*) → nuevo certificado | **Administración de Certificados Digitales** → agregar alias |
| Habilitar el servicio | En WSASS: **Crear autorización a servicio** | **Administrador de Relaciones de Clave Fiscal** → Nueva relación |

Detalles que conviene saber:

- **Renovar es lo mismo:** con un certificado por vencer, generá un pedido nuevo. El par
  vigente sigue facturando hasta que llega el `.crt` nuevo; en ese momento se reemplazan
  los dos archivos juntos.
- **Un solo pedido pendiente por ambiente y por servicio.** Generar otro lo reemplaza y
  **pierde la clave del anterior**: si su `.csr` ya está en ARCA, el `.crt` que vuelva no
  va a servir. El sistema pide confirmación.
- Para el **CTG y la Carta de Porte** (`wscpe`) el certificado puede ir a nombre de la
  persona que representa a la empresa: usá **su** CUIT y su nombre, no los de la empresa.
- La razón social va **sin tildes** (la `Ñ` se escribe `N`); el sistema las quita.
- La clave se guarda en el volumen de la instancia (`arca_certs/`), con permisos 0600, y
  entra en el respaldo igual que el resto del par.

El resto de la guía es el mismo trámite hecho a mano.

---

## Paso 1 — Generar la clave y el pedido a mano (en tu PC)

> Sólo si no podés usar el botón de arriba —un par ya hecho afuera, un producto sin la
> pantalla nueva—. Con el botón, este paso lo hace el sistema.

```bash
openssl genrsa -out clave.key 2048
```

```bash
openssl req -new -key clave.key -subj "/C=AR/O=NOMBRE_EMPRESA/CN=nombre_del_sistema/serialNumber=CUIT 20123456789" -out pedido.csr
```

Qué reemplazar:

- `O=NOMBRE_EMPRESA` → la razón social del contribuyente.
- `CN=nombre_del_sistema` → un nombre descriptivo, libre. Sirve para reconocerlo
  después en la lista de ARCA.
- `serialNumber=CUIT 20123456789` → el CUIT **sin guiones**, con la palabra `CUIT`
  adelante y un espacio.

> 🔴 **La clave se genera SIN contraseña, y no es un descuido.** El ticket de acceso a
> ARCA se pide sin que haya nadie mirando —de noche, desde el cron—, así que no hay
> dónde escribirla. Una clave protegida con passphrase se acepta el día que la subís y
> **falla al emitir el primer comprobante**. La pantalla ahora la rechaza al subirla y
> te dice por qué.
>
> Guías viejas de la familia recomendaban el paso contrario (`openssl pkcs8 … -v2 des3`).
> Estaba mal: no la sigas.

---

## Paso 2 — Subir el pedido a ARCA

1. Entrá a **https://www.arca.gob.ar** con clave fiscal **nivel 3 o superior**.
2. Buscá el servicio **"Administración de Certificados Digitales"**.
3. **Crear nuevo alias**: ponele un nombre y subí el `pedido.csr`.
4. Queda en "pendiente". Esperá a que pase a **Activo** — minutos u horas.
5. Cuando esté activo, **descargalo**. Es el `.crt`.

> ⚠️ **Lo que descargás es el `.crt`, no el `.csr`.** El `.csr` es lo que vos le
> mandaste a ARCA. Son los dos archivos de texto que empiezan con `-----BEGIN`, y
> confundirlos es el error más común de todos. La pantalla lo detecta al subirlo, pero
> conviene no llegar hasta ahí.

---

## Paso 3 — Habilitar el certificado para los servicios

**Sin esto no funciona nada**, y el error que devuelve ARCA no dice que falte.

En **"Administrador de Relaciones de Clave Fiscal"** → **Nueva Relación**, una vez por
cada servicio:

| Servicio | Para qué | ¿Obligatorio? |
|---|---|---|
| **wsfe** — Factura Electrónica | Emitir comprobantes | Sí |
| **ws_sr_padron_a13** — Consulta a Padrón Alcance 13 | El botón *Consultar ARCA* del alta de clientes: trae razón social, domicilio y condición frente al IVA | No, pero se nota |

En las dos: **Entidad** ARCA, **Representado** el CUIT del contribuyente, y
**Certificado** el alias que creaste en el paso 2.

> ARCA discontinuó el Padrón Alcance 4 (`ws_sr_padron_a4`). El vigente es el **13**.

---

## Paso 4 — Cargarlo en el sistema

En la instancia: **Configuración → ARCA**.

1. **CUIT** y **punto de venta** del contribuyente.
2. **Ambiente**: `homologación` para probar, `producción` para emitir de verdad.
3. Si generaste el pedido desde la pantalla, subí sólo el **certificado** (`.crt`): la clave
   ya está. Si lo hiciste a mano, subí el **certificado** (`.crt`) y la **clave** (`.key`).
4. Apretá **Probar**.

La pantalla valida al subir, no al emitir. Lo que rechaza y por qué:

| Mensaje | Qué pasó |
|---|---|
| *"no parece un certificado PEM…"* | Subiste el `.csr` en vez del `.crt`, o un archivo que no es ninguno de los dos |
| *"no parece una clave privada PEM…"* | Cambiaste de campo el certificado y la clave |
| *"la clave privada está protegida con contraseña"* | Ver el aviso del paso 1 |
| *"no es pareja de…"* | 🔑 Los dos archivos son válidos **y no van juntos**. Pasa cuando se genera una clave nueva y se sube el certificado viejo. ARCA lo rechazaría con un error genérico que no dice esto |

**Probar** es lo único que confirma que el paso 3 está hecho: autentica de verdad
contra ARCA. Un par perfecto al que nadie le habilitó `wsfe` pasa todas las
validaciones locales.

---

## Homologación y producción son dos certificados distintos

Son dos entornos separados de ARCA, cada uno con su certificado:

| | Homologación | Producción |
|---|---|---|
| Para qué | Probar | Emitir comprobantes fiscales reales |
| WSAA | `wsaahomo.afip.gov.ar` | `wsaa.afip.gov.ar` |
| WSFE | `wswhomo.afip.gov.ar` | `servicios1.afip.gov.ar` |

Hay que repetir los pasos 1 a 3 en cada uno. **Pasá a producción recién cuando una
factura de prueba salga con CAE en homologación.**

---

## El vencimiento, que es el que se olvida

🔑 **Los certificados de ARCA duran dos años, y el día que vencen la facturación deja
de andar sin que nadie haya tocado nada.** No hay aviso: un día el CAE deja de salir.

La pantalla de Configuración muestra **cuándo vence y cuántos días faltan**. Renovar es
repetir los pasos 1 a 3 y volver a subir el par.

> Guías viejas decían "generalmente 1 año". Son **dos**. Verificable en cualquier
> certificado emitido: `openssl x509 -in alias.crt -noout -dates`.

---

## Verificar a mano, si hace falta

```bash
openssl x509 -in alias.crt -noout -subject -dates
```

Que la clave y el certificado sean pareja — la salida tiene que ser **vacía**:

```bash
diff <(openssl x509 -in alias.crt -pubkey -noout) <(openssl pkey -in clave.key -pubout)
```

---

## Checklist

- [ ] Pedido generado desde la pantalla (o, a mano, `clave.key` **sin passphrase** y `pedido.csr` con el CUIT correcto en `serialNumber`)
- [ ] CSR subido a ARCA y certificado en estado **Activo**
- [ ] `.crt` descargado
- [ ] Relación con **wsfe** creada en Administrador de Relaciones
- [ ] Relación con **ws_sr_padron_a13** creada (opcional)
- [ ] Punto de venta dado de alta en ARCA
- [ ] Par cargado desde Configuración → ARCA, y **Probar** en verde
- [ ] Una factura de prueba con CAE en homologación
- [ ] Fecha de vencimiento anotada

---

## Seguridad

- `clave.key` **no entra a git ni viaja por mail.** Es la identidad digital del
  contribuyente: quien la tiene puede emitir comprobantes a su nombre. Con el pedido
  generado desde la pantalla ese riesgo no existe: la clave nace en el servidor y no se
  puede descargar.
- El `.gitignore` de todos los productos ya excluye `*.key`, `*.pem` y el directorio de
  certificados. Si aparece un archivo nuevo con pinta de credencial, va al `.gitignore`
  **antes** de cualquier `git add`.
- ⚠️ En [[libracargo]] el par se guarda **en la base**, para que entre en el dump del
  backup. La contracara: el ZIP de backup que el cliente descarga lleva adentro la clave
  privada. Es su propia clave y su propio backup, pero conviene que no viaje por mail.
