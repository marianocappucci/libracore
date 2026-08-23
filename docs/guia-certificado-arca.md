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

Los dos se suben desde la pantalla de Configuración. **No hace falta entrar al
servidor ni copiar nada a mano** — y no se puede: la pantalla es la única vía.

---

## Paso 1 — Generar la clave y el pedido (en tu PC)

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
3. Subí el **certificado** (`.crt`) y la **clave** (`.key`).
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

- [ ] `clave.key` generada **sin passphrase**
- [ ] `pedido.csr` generado con el CUIT correcto en `serialNumber`
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
  contribuyente: quien la tiene puede emitir comprobantes a su nombre.
- El `.gitignore` de todos los productos ya excluye `*.key`, `*.pem` y el directorio de
  certificados. Si aparece un archivo nuevo con pinta de credencial, va al `.gitignore`
  **antes** de cualquier `git add`.
- ⚠️ En [[libracargo]] el par se guarda **en la base**, para que entre en el dump del
  backup. La contracara: el ZIP de backup que el cliente descarga lleva adentro la clave
  privada. Es su propia clave y su propio backup, pero conviene que no viaje por mail.
