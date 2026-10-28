"""Avisos por mail y WhatsApp para todos los productos (ADR-090).

Hasta acá ningún producto de la familia mandaba WhatsApp, LibraClub tenía su propio motor de avisos y GestioLibra y MedLibra
escribían los recordatorios al log. Esto es la pieza de fondo, que vive en el motor (el arreglo de fondo va siempre acá): **cómo
sale un mensaje y que no salga dos veces**. Qué hay que avisar (las reservas de mañana, los turnos sin confirmar) y qué texto
lleva sigue siendo del producto.

    from libracore.avisos import Mensaje, despachar, proveedores

    prov = proveedores(smtp_config)["whatsapp"]          # apagado, registro o Evolution, según la configuración
    envio = despachar(prov, Mensaje(telefono_para_whatsapp(cliente.telefono), cuerpo="*Turno confirmado*\\n..."),
                      clave=f"reserva:{reserva.id}:confirmacion", tipo="confirmacion")
    envio.estado        # enviado | registrado | fallido | rechazado | omitido | no_disponible | en_nodo

- `Mensaje(destino, asunto, cuerpo, plantilla, variables)`: el contenido, para cualquier canal.
- `Proveedor` (`canal`, `nombre`, `simulado`, `disponible()`, `enviar()`): `ProveedorSmtp` (el SMTP que la instancia ya tiene),
  `ProveedorEvolution` (WhatsApp por Evolution API), `ProveedorRegistro` (modo prueba: anota y no manda) y `ProveedorApagado`.
- `despachar` / `reintentar_fallidos` / `cerrado` / `cerradas` / `omitir`: el despachador, con su registro en la tabla `avisos_envios`
  (`libracore.db.avisos_envios`), una clave única por aviso, intentos y reintento.
- `telefono_para_whatsapp`: el teléfono tal como lo piden los proveedores. Ante la duda, vacío.
- `libracore.avisos.configuracion`: el modo de cada canal y la conexión a Evolution, con la apikey en el almacén cifrado.
- `libracore.avisos.agenda.NotificacionPorAvisos`: el adaptador de `libragenda.NotificationPort` para GestioLibra y MedLibra.
- `libracore.avisos_router.build_avisos_router`: la API de configuración, «probar envío» y el registro, que cada producto monta.
"""
from libracore.avisos.configuracion import (
    ConfigWhatsApp,
    ErrorDeConfiguracion,
    proveedor_email,
    proveedor_whatsapp,
    proveedores,
)
from libracore.avisos.despachador import (
    EN_NODO,
    ESPERA_ENTRE_INTENTOS,
    MAX_INTENTOS,
    NO_DISPONIBLE,
    Envio,
    Resumen,
    cerradas,
    cerrado,
    despachar,
    omitir,
    reintentar_fallidos,
)
from libracore.avisos.mensaje import (
    CANAL_EMAIL,
    CANAL_WHATSAPP,
    CANALES,
    Mensaje,
    Proveedor,
    Resultado,
)
from libracore.avisos.proveedores import (
    ProveedorApagado,
    ProveedorEvolution,
    ProveedorRegistro,
    ProveedorSmtp,
)
from libracore.avisos.telefono import es_telefono_normalizado, telefono_para_whatsapp
