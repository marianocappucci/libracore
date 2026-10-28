# Avisos por mail y WhatsApp: cómo se usan

`libracore.avisos` (ADR-090). El motor sabe **cómo sale un mensaje y que no salga dos veces**; qué hay que avisar y con qué texto es del
producto. Esta página es para quien lo adopta.

## Mandar un aviso

```python
from libracore.avisos import Mensaje, despachar, proveedores, telefono_para_whatsapp

prov = proveedores(smtp_config)          # {"email": ..., "whatsapp": ...}; smtp_config = el resolver de SMTP del producto
whatsapp = prov["whatsapp"]              # apagado, registro o Evolution, según la configuración de la instancia

envio = despachar(
    whatsapp,
    Mensaje(destino=telefono_para_whatsapp(cliente.telefono), cuerpo="*Turno confirmado*\nMañana 19:00"),
    clave=f"reserva:{reserva.id}:confirmacion",   # de qué hecho es el aviso: la misma clave nunca sale dos veces
    tipo="confirmacion",                            # sólo sirve para filtrar el listado
)
envio.estado   # enviado | registrado | fallido | rechazado | omitido | no_disponible | en_nodo
```

- **`clave`** es lo que evita el doble envío. Tiene que ser la misma cada vez que el barrido vuelve sobre el mismo hecho y distinta
  entre hechos (incluí la anticipación del recordatorio: `reserva:41:recordatorio:24` y `reserva:41:recordatorio:2`). Hasta 200 caracteres.
  **Ids, nunca el teléfono ni el mail**: la clave aparece en los logs.
- `despachar` es síncrono y espera hasta el timeout del proveedor (10 s Evolution, 30 s SMTP): desde un cron o una ruta `def`, no desde un `async def`.
- **`destino`** de WhatsApp: el teléfono ya normalizado (`telefono_para_whatsapp`); vacío si no se pudo → `despachar` lo deja `omitido`.
  El proveedor de Evolution rechaza lo que no tenga país.
- **`plantilla`** y **`variables`**: opcionales. Evolution no las usa; la API de Meta las va a pedir. Se guardan en el registro.
- `no_disponible`: el canal está apagado o sin configurar. No se escribe nada en el registro.
- `en_nodo`: el proceso es un nodo de LibraEdge. **El nodo no manda**; el barrido corre en el central.

## Estados de un aviso

| Estado | Qué es | ¿Se vuelve a intentar? |
|---|---|---|
| `enviando` | alguien lo está mandando ahora | no; si pasan 10 minutos se lo da por abandonado |
| `enviado` | el proveedor lo aceptó | no |
| `registrado` | un proveedor **simulado** lo anotó sin mandar nada | sí, con un proveedor real (con uno simulado, no) |
| `fallido` | no salió (timeout, servidor caído, apikey mala) | sí, hasta 3 intentos |
| `rechazado` | el destino no sirve (número sin WhatsApp) | no |
| `omitido` | no correspondía mandarlo (sin teléfono, pidió no recibir) | no |

## Un barrido

```python
from libracore.avisos import cerradas, despachar, reintentar_fallidos, omitir, Resumen

whatsapp = proveedores(smtp_config)["whatsapp"]
candidatos = [...]                                     # lo que el producto deduce de sus tablas
ya = cerradas(whatsapp, [clave(c) for c in candidatos])  # una consulta por tanda de 500
resumen = Resumen()
for c in candidatos:
    if clave(c) in ya:
        continue
    destino = telefono_para_whatsapp(c.cliente.telefono)
    if not destino:
        resumen = resumen.sumar(omitir("whatsapp", clave(c), motivo="El cliente no tiene un teléfono que WhatsApp reconozca."))
        continue
    resumen = resumen.sumar(despachar(whatsapp, Mensaje(destino, cuerpo=redactar(c)), clave=clave(c), tipo=c.tipo))
reintentar_fallidos(whatsapp)                           # los fallidos de las últimas 24 horas, con el mensaje guardado
```

**El script de un cron (`docker exec`) tiene que enchufar el almacén de secretos** (`config_manager.usar_almacen_de_secretos`) antes de
despachar, y configurar `core` (`libracore.db.core.configure`). Sin el almacén, Evolution no tiene la `apikey`:
`configuracion.motivo_de_whatsapp()` lo dice.

## Montar la configuración

```python
from libracore.avisos_router import build_avisos_router

app.include_router(build_avisos_router(
    require_admin=require_admin,                                   # obligatorio: no hay default
    smtp_config=lambda: resolver_smtp_config(auth_sessions),       # el mismo resolver del envío de comprobantes
))
```

| Ruta (prefijo `/api/avisos`) | |
|---|---|
| `GET /config` | `{"whatsapp": {modo, modos, url, instancia, apikey_cargada, disponible, motivo}, "email": {disponible}}` |
| `PUT /config` | `{modo?, url?, instancia?, apikey?, quitar_apikey?}`. `apikey` vacía = no la toques. Devuelve lo mismo que el `GET` |
| `POST /probar` | `{canal: "whatsapp"\|"email", destino}`: manda un texto fijo. 200, 409 `no_disponible`, 422 (teléfono), 502 (falló el proveedor) |
| `GET /envios` | el registro: `?canal=&estado=&tipo=&limite=` |

La `apikey` **nunca vuelve** por la API. En un nodo, `PUT` y `probar` dan 409 `solo_en_el_central`.

## Conectar WhatsApp a una instancia

1. `PUT /config {"modo": "registro"}`: los avisos se **anotan** (`registrado`) y no sale nada. Se miran en `GET /envios`.
2. `PUT /config {"url": "https://evolution.ejemplo.example", "instancia": "club-demo", "apikey": "..."}`.
3. `POST /probar {"destino": "11 5555-1234"}` con tu propio número.
4. `PUT /config {"modo": "evolution"}`. Lo que quedó `registrado` en el paso 1 se manda en el próximo barrido.

Evolution es `POST {url}/message/sendText/{instancia}` con el header `apikey` y `{"number", "text"}`. Un 400 con `exists: false` es
«el número no tiene WhatsApp» (`rechazado`); cualquier otro error es `fallido` y se reintenta. Una demo (`DEMO_MODE=1`) nunca manda.

## GestioLibra y MedLibra (`libragenda`)

```python
from libracore.avisos.agenda import NotificacionPorAvisos

app.state.reminder_dispatcher = ReminderDispatcher(
    appointment_repository, reminder_repository,
    NotificacionPorAvisos(
        lambda: proveedores(smtp_config),
        contacto=lambda client_id: {"email": ..., "telefono": ...},     # del paciente
        redactar=lambda notificacion, canal: Mensaje("", asunto="Recordatorio de turno", cuerpo=...),
    ),
    DEFAULT_REMINDER_POLICIES,
)
```

`send` no levanta por un fallo de envío (cortaría la tanda de `dispatch`): el fallo queda `fallido` y lo reintenta
`reintentar_fallidos(proveedor)`, que corre después de `dispatch` en el mismo cron.

## Lo que el registro guarda

El texto del mensaje y el teléfono o el mail del destinatario, tal como se armaron, para poder contestar «¿le llegó?». Es información de
un cliente: el listado es sólo de administrador y el resto de los logs no lleva ni el teléfono ni el texto.
