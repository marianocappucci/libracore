"""
Rutas del backoffice: listar, alta, editar, plan, estado, backup y baja de
clientes. Migrado a libracore.admin como factory (Fase 4 de LibraCore,
backoffice compartido — código ya idéntico entre productos) — recibe
`auth`/`services`/`templates` ya configurados por el shim de cada
producto, en vez de resolverlos por nombre de módulo genérico.
"""
from typing import Annotated

from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import RedirectResponse


def create_clientes_router(auth, services, templates) -> APIRouter:
    router = APIRouter()
    Auth = Annotated[str, Depends(auth.require_login)]

    @router.get("/")
    def listar(request: Request, user: Auth, msg: str = "", error: str = ""):
        return templates.TemplateResponse(request, "clientes/list.html", {
            "clientes": services.listar_clientes(),
            "planes": services.planes_info(),
            "msg": msg, "error": error,
        })

    @router.get("/clientes/nuevo")
    def nuevo_form(request: Request, user: Auth, error: str = ""):
        return templates.TemplateResponse(request, "clientes/form.html", {
            "planes": services.planes_info(),
            "error": error,
        })

    @router.post("/clientes/nuevo")
    def nuevo_submit(
        request: Request, user: Auth,
        nombre: str = Form(...),
        slug: str = Form(""),
        domain: str = Form(""),
        port: str = Form(""),
        admin_user: str = Form("admin"),
        admin_password: str = Form(""),
        plan: str = Form("basico"),
        setup_npm: str = Form(""),
    ):
        try:
            info = services.crear_cliente(
                nombre=nombre, slug=slug, domain=domain,
                port=int(port) if port.strip() else 0,
                admin_user=admin_user, admin_password=admin_password,
                plan=plan, setup_npm=bool(setup_npm),
            )
        except services.ServiceError as e:
            return RedirectResponse(f"/clientes/nuevo?error={e}", status_code=303)
        # Mostrar credenciales generadas una sola vez, en el detalle.
        return RedirectResponse(
            f"/clientes/{info['slug']}?msg=creado&pass={info['admin_password']}",
            status_code=303,
        )

    @router.get("/clientes/{slug}")
    def detalle(request: Request, user: Auth, slug: str, msg: str = "", error: str = ""):
        cliente = services.get_cliente(slug)
        if not cliente:
            return RedirectResponse("/?error=Cliente no encontrado", status_code=303)
        return templates.TemplateResponse(request, "clientes/detail.html", {
            "c": cliente,
            "planes": services.planes_info(),
            "msg": msg, "error": error,
            "nueva_pass": request.query_params.get("pass", ""),
        })

    @router.post("/clientes/{slug}/editar")
    def editar(request: Request, user: Auth, slug: str,
               nombre: str = Form(""), domain: str = Form("")):
        try:
            services.editar_cliente(slug, nombre, domain)
        except services.ServiceError as e:
            return RedirectResponse(f"/clientes/{slug}?error={e}", status_code=303)
        return RedirectResponse(f"/clientes/{slug}?msg=editado", status_code=303)

    @router.post("/clientes/{slug}/plan")
    def cambiar_plan(request: Request, user: Auth, slug: str, plan: str = Form(...)):
        try:
            services.set_plan(slug, plan)
        except services.ServiceError as e:
            return RedirectResponse(f"/clientes/{slug}?error={e}", status_code=303)
        return RedirectResponse(f"/clientes/{slug}?msg=plan", status_code=303)

    @router.post("/clientes/{slug}/estado/{accion}")
    def estado(request: Request, user: Auth, slug: str, accion: str):
        try:
            services.accion_estado(slug, accion)
        except services.ServiceError as e:
            return RedirectResponse(f"/clientes/{slug}?error={e}", status_code=303)
        return RedirectResponse(f"/clientes/{slug}?msg=estado", status_code=303)

    @router.post("/clientes/{slug}/backup")
    def backup(request: Request, user: Auth, slug: str):
        try:
            services.backup_cliente(slug)
        except services.ServiceError as e:
            return RedirectResponse(f"/clientes/{slug}?error={e}", status_code=303)
        return RedirectResponse(f"/clientes/{slug}?msg=backup", status_code=303)

    @router.post("/clientes/{slug}/eliminar")
    def eliminar(request: Request, user: Auth, slug: str, confirmar: str = Form("")):
        if confirmar.strip() != slug:
            return RedirectResponse(
                f"/clientes/{slug}?error=Escribí el slug exacto para confirmar la baja",
                status_code=303,
            )
        try:
            services.eliminar_cliente(slug, hacer_backup=True)
        except services.ServiceError as e:
            return RedirectResponse(f"/clientes/{slug}?error={e}", status_code=303)
        return RedirectResponse("/?msg=eliminado", status_code=303)

    return router
