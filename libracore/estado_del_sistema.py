"""La barra de estado del pie (ADR-075): una sola consulta con lo que la pantalla tiene que decir de la instancia.

`GET /api/estado-del-sistema` junta, en una respuesta chica, cinco cosas que ya se sabían por separado y que nadie
miraba hasta que fallaban:

1. **LibraEdge.** En un nodo: si ve al central (`nodo_offline.ConexionDelNodo`, ADR-074), cuántas operaciones esperan
   subir (el `pendientes` del archivo de estado del proceso del nodo, `LIBRAEDGE_ESTADO`) y cuántas ventas esperan la
   factura en la bandeja (`venta_facturacion.ventas_a_facturar`). En el central con nodos: cuántos hay y cuántos
   están sin noticias (la lista la da el producto, que es el que depende de `libraedge`). Sin LibraEdge, `null`.
2. **Usuario y sucursal** de quien mira.
3. **Versión** de la instancia: `LIBRA_VERSION`, el tag de la imagen que escribe el panel (ADR-045), y la del motor.
4. **ARCA:** el certificado que vence pronto (`DIAS_AVISO_CERTIFICADO`) o ya venció, de la facturación y de los
   servicios (`arca_credenciales_servicio`). Una instancia sin certificado en disco (no factura con ARCA, una demo,
   un nodo, que firma con el ticket prestado) da `null`.
5. **Respaldo:** cuándo fue el último ZIP de `data/backups`, si el respaldo de la noche falló o no corrió
   (`respaldo_nocturno`) y si la copia externa (`resguardo_estado`) falló o está vieja.

El motor no sabe de roles ni de sucursales: el producto monta el router con su `usuario_actual` y, si hace falta, su
`es_admin` y su `sucursal`. Todo lo demás sale de acá, igual para los ocho.

## Permisos

- **Todos:** LibraEdge del nodo (el cajero es el que tiene que saber que está sin conexión), usuario, sucursal y la
  versión del producto.
- **Sólo administradores:** el detalle de ARCA, el respaldo, los nodos del central y la versión del motor.
- **El resto, resumido:** si el certificado **ya venció** (la facturación no anda), `arca` es `{"nivel": "error",
  "vencido": true}` sin fechas, para que el cajero sepa por qué no factura y avise.

Nada secreto: ni rutas, ni CUIT, ni el texto de un error (el de la copia externa puede traer lo que contestó la nube
del cliente). Para eso están las pantallas de Configuración.

## Cada bloque, por su cuenta

Un bloque que falla (un certificado ilegible, la base caída) se registra en el log y sale `null`: la barra no puede
romper la pantalla, ni un bloque roto llevarse a los demás.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from libracore.nodo_offline import FUERA_DE_NODO, VARIABLE_ESTADO, ConexionDelNodo

logger = logging.getLogger(__name__)

#: Días antes del vencimiento del certificado de ARCA desde los que se avisa. ARCA tarda en emitir uno nuevo y el
#: pedido lo tiene que hacer el cliente con su clave fiscal: un mes alcanza para hacerlo sin apuro.
DIAS_AVISO_CERTIFICADO = 30
#: Horas desde el último respaldo para avisar que es viejo. El del panel corre una vez por noche: 36 y no 24 para que
#: una noche que se corre un poco no avise de más (el mismo criterio que `resguardo_estado.HORAS_FRESCURA`).
HORAS_RESPALDO_VIEJO = 36
#: Cada cuánto le conviene a la pantalla volver a preguntar. En un nodo, cada minuto (el proceso del nodo sincroniza
#: cada minuto y el cajero tiene que ver el corte); fuera de un nodo, cada cinco.
REFRESCO_EN_UN_NODO = 60
REFRESCO_FUERA_DE_UN_NODO = 300

OK, ATENCION, ERROR = "ok", "atencion", "error"
_ORDEN = {OK: 0, ATENCION: 1, ERROR: 2}

#: El rol de administrador de la familia (libraauth). El producto pasa su propio `es_admin` si dice otra cosa.
ROL_ADMIN = "admin"


def _peor(niveles: Iterable[str]) -> str:
    return max(niveles, key=lambda n: _ORDEN.get(n, 0), default=OK)


def _a_iso(fecha: datetime | None) -> str | None:
    return fecha.astimezone(UTC).isoformat() if fecha else None


# ── LibraEdge ───────────────────────────────────────────────────────────────


def leer_estado_del_nodo(ruta: str | None = None) -> dict | None:
    """El archivo de estado del proceso del nodo (`libraedge.nodo.escribir_estado`), o `None` si no hay o no se lee.

    Sin `ruta`, la de `LIBRAEDGE_ESTADO`. Es el mismo archivo que lee `nodo_offline.esta_en_linea`."""
    ruta = ruta if ruta is not None else (os.environ.get(VARIABLE_ESTADO) or "").strip() or None
    if not ruta:
        return None
    try:
        with open(ruta, encoding="utf-8") as archivo:
            estado = json.load(archivo)
    except (OSError, ValueError):
        return None
    return estado if isinstance(estado, dict) else None


def _entero(valor) -> int | None:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0 else None


def estado_libraedge_del_nodo(conexion: ConexionDelNodo, *, a_facturar: Callable[[], int] | None = None,
                              ruta_estado: str | None = None) -> dict | None:
    """El bloque de un nodo: `{"rol": "nodo", "en_linea", "pendientes_de_subir", "a_facturar",
    "ultima_sincronizacion", "nivel"}`. Fuera de un nodo, `None`.

    `pendientes_de_subir` es `None` si el proceso del nodo todavía no escribió su estado (no se sabe, que no es cero).
    Sin conexión es `atencion` y no `error`: el nodo sigue vendiendo, y eso es lo que tiene que entender el cajero."""
    if not conexion.es_nodo():
        return None
    en_linea = not conexion.sin_conexion()
    estado = leer_estado_del_nodo(ruta_estado) or {}
    facturar = 0
    if a_facturar is not None:
        try:
            facturar = int(a_facturar() or 0)
        except Exception:
            logger.exception("Estado del sistema: no se pudo contar la bandeja del nodo")
            facturar = 0
    ultima = estado.get("ultima_sincronizacion_ok")
    return {
        "rol": "nodo",
        "en_linea": en_linea,
        "pendientes_de_subir": _entero(estado.get("pendientes")),
        "a_facturar": facturar,
        "ultima_sincronizacion": ultima if isinstance(ultima, str) and ultima.strip() else None,
        "nivel": OK if en_linea else ATENCION,
    }


#: Los `estado` de `libraedge.pantalla_del_central.nodos_de_la_instancia` que cuentan como «sin noticias».
_SIN_NOTICIAS = frozenset({"sin_noticias", "nunca"})


def resumen_de_nodos_del_central(nodos: Iterable[dict]) -> dict | None:
    """El bloque del central: `{"rol": "central", "nodos", "sin_noticias", "nivel"}`. Sin nodos activos, `None`.

    `nodos` es la lista de `libraedge.pantalla_del_central.nodos_de_la_instancia` (la misma de «Sucursal sin
    internet»): se usan sólo `activo` y `estado`."""
    activos = [n for n in nodos if isinstance(n, dict) and n.get("activo")]
    if not activos:
        return None
    sin_noticias = sum(1 for n in activos if n.get("estado") in _SIN_NOTICIAS)
    return {"rol": "central", "nodos": len(activos), "sin_noticias": sin_noticias,
            "nivel": ATENCION if sin_noticias else OK}


# ── ARCA ────────────────────────────────────────────────────────────────────


def _pares_en_disco() -> list[tuple[str, str]]:
    """`[(servicio, ruta_del_certificado)]` de lo que la instancia usa para hablar con ARCA, sólo lo que está en disco.

    La facturación con el par del ambiente del selector (`arca_credenciales.paths_en_disco`, que en una demo no da
    nada) y los demás servicios del mismo ambiente (`wscpe`, ADR-032)."""
    from libracore import arca_credenciales
    from libracore.db import arca_config as db_arca
    from libracore.db import arca_credenciales_servicio as db_servicio

    pares: list[tuple[str, str]] = []
    vistos: set[str] = set()

    def sumar(servicio: str, cert: str) -> None:
        if cert and os.path.isfile(cert) and os.path.realpath(cert) not in vistos:
            vistos.add(os.path.realpath(cert))
            pares.append((servicio, cert))

    for cfg in db_arca.obtener_todas_arca_configs():
        cert, _ = arca_credenciales.paths_en_disco(cfg)
        sumar("facturacion", cert)
        ambiente = cfg.get("ambiente") or ""
        try:
            servicios = db_servicio.listar_de_empresa(cfg.get("empresa") or "")
        except Exception:  # una base sin la tabla de servicios (anterior a la 0022) no tiene servicios
            servicios = []
        for fila in servicios:
            servicio = str(fila.get("servicio") or "")
            if not servicio or (fila.get("ambiente") and fila.get("ambiente") != ambiente):
                continue
            cert, _ = arca_credenciales.paths_en_disco_de_servicio(cfg.get("empresa") or "", servicio, ambiente)
            sumar(servicio, cert)
    return pares


def estado_arca(*, dias_aviso: int = DIAS_AVISO_CERTIFICADO, ahora: datetime | None = None,
                pares: Callable[[], list[tuple[str, str]]] = _pares_en_disco) -> dict | None:
    """El certificado de ARCA que más apura: `{"nivel", "servicio", "vence", "dias_para_vencer", "vencido",
    "certificados"}`. Sin ningún certificado en disco, `None` (la instancia no factura con ARCA).

    `vence` es la fecha en ISO (`aaaa-mm-dd`). Un certificado ilegible es `error` con `vence: null`: tampoco firma."""
    from libracore import arca_certificados

    ahora = ahora or datetime.now(UTC)
    filas = []
    for servicio, cert in pares():
        try:
            datos = arca_certificados.leer_certificado_de_archivo(cert)
        except arca_certificados.ArchivoInvalido:
            filas.append({"nivel": ERROR, "servicio": servicio, "vence": None, "dias_para_vencer": None,
                          "vencido": False, "ilegible": True})
            continue
        dias = (datos.vence - ahora).days
        vencido = datos.vence < ahora
        nivel = ERROR if vencido else ATENCION if dias < dias_aviso else OK
        filas.append({"nivel": nivel, "servicio": servicio, "vence": datos.vence.date().isoformat(),
                      "dias_para_vencer": dias, "vencido": vencido, "ilegible": False})
    if not filas:
        return None
    # El que más apura: el peor nivel y, dentro de él, el que vence antes (un ilegible primero).
    peor = min(filas, key=lambda f: (-_ORDEN[f["nivel"]], f["dias_para_vencer"] if f["dias_para_vencer"] is not None
                                     else -10**6))
    return {**peor, "certificados": len(filas)}


# ── Respaldo ────────────────────────────────────────────────────────────────


def estado_respaldo(backups_dir, *, horas: int = HORAS_RESPALDO_VIEJO, ahora: datetime | None = None) -> dict:
    """`{"nivel", "ultimo", "horas_desde_el_ultimo", "motivo", "nocturno", "externo"}` de la carpeta de respaldos.

    - `ultimo`: el ZIP más nuevo **por fecha** (no por nombre: `backup_manual_…` ordena después de
      `backup_automatico_…`), en ISO con zona. Sin ninguno, `null` y `atencion`.
    - `nocturno`: el último intento del respaldo de la noche (`respaldo_nocturno`, lo escribe `backup-all`),
      `{"ultimo_intento", "fallo"}` sin el texto del error; `null` si nunca corrió en esta instancia (dev, una PC).
    - `motivo`, por qué el bloque no está en `ok` (`null` si lo está), para que la pantalla diga a quién avisar:
      `"fallo"` (el último intento falló y es posterior al último ZIP: `error`), `"sin_respaldos"`, `"no_corrio"`
      (el último ZIP es viejo y no hubo intento en ese plazo: es el cron o el host) y `"viejo"` (hubo intento
      bueno pero el ZIP es viejo, que no debería pasar).
    - `externo`: la copia a la nube del cliente (`resguardo_estado.resumen`), `null` si no está contratada;
      `{"al_dia", "fallo", "ultimo"}`, sin el texto del error. Si la última subida falló, el bloque es `error`.
    """
    from libracore.resguardo_estado import leer_estado, resumen
    from libracore.respaldo_nocturno import leer_intento

    ahora = ahora or datetime.now(UTC)
    carpeta = Path(backups_dir)
    ultimo: datetime | None = None
    if carpeta.is_dir():
        for archivo in carpeta.glob("*.zip"):
            try:
                cuando = datetime.fromtimestamp(archivo.stat().st_mtime, UTC)
            except OSError:
                continue
            if ultimo is None or cuando > ultimo:
                ultimo = cuando
    horas_desde = round((ahora - ultimo).total_seconds() / 3600, 1) if ultimo else None

    intento = leer_intento(carpeta)
    nocturno = None
    if intento:
        nocturno = {"ultimo_intento": _a_iso(intento["cuando"]), "fallo": not intento["ok"]}
    if intento and not intento["ok"] and (ultimo is None or intento["cuando"] >= ultimo):
        nivel, motivo = ERROR, "fallo"
    elif ultimo is None:
        nivel, motivo = ATENCION, "sin_respaldos"
    elif horas_desde > horas:
        reciente = intento and (ahora - intento["cuando"]).total_seconds() / 3600 <= horas
        nivel, motivo = ATENCION, "viejo" if reciente else "no_corrio"
    else:
        nivel, motivo = OK, None

    externo = None
    datos = resumen(carpeta)
    if datos.get("contratado"):
        crudo = leer_estado(carpeta) or {}
        fallo = crudo.get("ok") is False
        externo = {"al_dia": datos.get("al_dia") is True, "fallo": fallo,
                   "ultimo": (datos.get("detalle") or {}).get("cuando")}
        nivel = _peor([nivel, ERROR if fallo else OK if externo["al_dia"] else ATENCION])
    return {"nivel": nivel, "ultimo": _a_iso(ultimo), "horas_desde_el_ultimo": horas_desde, "motivo": motivo,
            "nocturno": nocturno, "externo": externo}


# ── Versión y usuario ───────────────────────────────────────────────────────


def version_de_la_instancia(version: str | Callable[[], str | None] | None = None) -> str | None:
    """La versión del producto: la que pasa el producto o, si no, `LIBRA_VERSION` (el tag de la imagen, ADR-045).
    `None` en una instancia que no la tiene (dev, una PC de desarrollo)."""
    valor = version() if callable(version) else version
    valor = (valor or os.environ.get("LIBRA_VERSION") or "").strip()
    return valor or None


def version_del_motor() -> str | None:
    from libracore import __version__

    return __version__ or None


def es_admin_por_defecto(usuario: dict) -> bool:
    """`role == "admin"` (libraauth). El visitante de una demo pública no es administrador aunque vea sus menús."""
    if usuario.get("demo_readonly"):
        return False
    return usuario.get("role") == ROL_ADMIN or usuario.get("rol") == ROL_ADMIN


def sucursal_por_defecto(usuario: dict) -> str | None:
    """La sucursal que trae el usuario (`sucursal_nombre` o `sucursal` como texto), si la trae."""
    for clave in ("sucursal_nombre", "sucursal"):
        valor = usuario.get(clave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    return None


def _nombre(usuario: dict) -> str | None:
    for clave in ("nombre", "name", "username"):
        valor = usuario.get(clave)
        if isinstance(valor, str) and valor.strip():
            return valor.strip()
    return None


# ── El router ───────────────────────────────────────────────────────────────


def _bloque(que: str, calcular: Callable[[], Any]):
    try:
        return calcular()
    except Exception:
        logger.exception("Estado del sistema: no se pudo armar el bloque %s", que)
        return None


def armar_estado_del_sistema(
    usuario: dict,
    *,
    es_admin: Callable[[dict], bool] = es_admin_por_defecto,
    sucursal: Callable[[dict], str | None] = sucursal_por_defecto,
    version: str | Callable[[], str | None] | None = None,
    conexion: ConexionDelNodo = FUERA_DE_NODO,
    a_facturar: Callable[[], int] | None = None,
    ruta_estado_del_nodo: str | None = None,
    nodos_del_central: Callable[[], Iterable[dict]] | None = None,
    backups_dir=None,
    factura_con_arca: Callable[[], bool] | None = None,
) -> dict:
    """La respuesta de `GET /api/estado-del-sistema` para `usuario`. Ver el docstring del módulo."""
    usuario = usuario or {}
    admin = bool(_bloque("permisos", lambda: es_admin(usuario)))
    es_nodo = bool(_bloque("libraedge", conexion.es_nodo))

    libraedge = None
    if es_nodo:
        libraedge = _bloque("libraedge", lambda: estado_libraedge_del_nodo(
            conexion, a_facturar=a_facturar, ruta_estado=ruta_estado_del_nodo))
    elif admin and nodos_del_central is not None:
        libraedge = _bloque("libraedge", lambda: resumen_de_nodos_del_central(nodos_del_central() or []))

    # En un nodo no hay certificado (firma con el ticket del central) ni respaldo propio (lo suyo sube al central).
    arca = None
    if not es_nodo and (factura_con_arca is None or _bloque("arca", factura_con_arca)):
        arca = _bloque("arca", estado_arca)
        if arca is not None and not admin:
            arca = {"nivel": ERROR, "vencido": True} if arca.get("vencido") else None
    respaldo = None
    if admin and not es_nodo and backups_dir is not None:
        respaldo = _bloque("respaldo", lambda: estado_respaldo(backups_dir() if callable(backups_dir) else backups_dir))

    return {
        "usuario": {"nombre": _nombre(usuario), "rol": usuario.get("role") or usuario.get("rol"),
                    "sucursal": _bloque("sucursal", lambda: sucursal(usuario))},
        "version": {"producto": _bloque("version", lambda: version_de_la_instancia(version)),
                    "motor": version_del_motor() if admin else None},
        "libraedge": libraedge,
        "arca": arca,
        "respaldo": respaldo,
        "refrescar_en_segundos": REFRESCO_EN_UN_NODO if es_nodo else REFRESCO_FUERA_DE_UN_NODO,
    }


def build_estado_del_sistema_router(
    *,
    usuario_actual: Callable[..., Any],
    es_admin: Callable[[dict], bool] = es_admin_por_defecto,
    sucursal: Callable[[dict], str | None] = sucursal_por_defecto,
    version: str | Callable[[], str | None] | None = None,
    ventas=None,
    conexion: ConexionDelNodo | None = None,
    a_facturar: Callable[[], int] | None = None,
    nodos_del_central: Callable[[], Iterable[dict]] | None = None,
    backups_dir=None,
    factura_con_arca: Callable[[], bool] | None = None,
    prefix: str = "/api",
) -> APIRouter:
    """`GET {prefix}/estado-del-sistema`: la barra de estado del pie (ADR-075). **Sin guarda propia**: la pone
    `usuario_actual` (cualquier usuario logueado; lo que ve cada rol lo decide `es_admin`).

    - `ventas`: el `PuertoDeVentas` del producto (el de `build_facturacion_del_nodo_router`): de ahí salen la conexión
      del nodo y la cuenta de la bandeja. Un producto sin ventas pasa `conexion` (y `a_facturar`) o nada.
    - `nodos_del_central`: en el central, la lista de nodos de `libraedge.pantalla_del_central.nodos_de_la_instancia`.
    - `backups_dir`: la carpeta de respaldos (la misma de `build_backup_router`), o un callable que la devuelva.
    - `factura_con_arca`: si la instancia factura con ARCA (p. ej. el módulo de facturación prendido). Sin él, se mira
      sólo si hay certificado en disco.
    - `version`: la del producto, si no alcanza con `LIBRA_VERSION`.
    """
    if conexion is None:
        conexion = getattr(ventas, "conexion", None) or FUERA_DE_NODO
    if a_facturar is None and ventas is not None:
        from libracore.venta_facturacion import ventas_a_facturar

        def a_facturar() -> int:
            return ventas_a_facturar(ventas)

    router = APIRouter(prefix=prefix, tags=["estado"])

    @router.get("/estado-del-sistema")
    def estado_del_sistema(user: dict = Depends(usuario_actual)):
        """Lo que muestra la barra de estado. Una consulta al cargar el layout y después cada
        `refrescar_en_segundos` (o al volver a la pestaña); nunca una por render."""
        return armar_estado_del_sistema(
            user if isinstance(user, dict) else {}, es_admin=es_admin, sucursal=sucursal, version=version,
            conexion=conexion, a_facturar=a_facturar, nodos_del_central=nodos_del_central,
            backups_dir=backups_dir, factura_con_arca=factura_con_arca)

    return router


__all__ = [
    "ATENCION", "DIAS_AVISO_CERTIFICADO", "ERROR", "HORAS_RESPALDO_VIEJO", "OK", "armar_estado_del_sistema",
    "build_estado_del_sistema_router", "es_admin_por_defecto", "estado_arca", "estado_libraedge_del_nodo",
    "estado_respaldo", "leer_estado_del_nodo", "resumen_de_nodos_del_central", "sucursal_por_defecto",
    "version_de_la_instancia", "version_del_motor",
]
