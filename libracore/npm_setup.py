"""
Configuración interactiva de la conexión a Nginx Proxy Manager (NPM).

Requiere que `libracore.npm_api.configure(config_file=...)` ya se haya
llamado (lo hace el shim `scripts/npm_setup.py` de cada producto) antes de
invocar `main()`.
"""
import sys

from .npm_api import NPMClient, config_file, load_config, save_config


def main(product_name: str):
    print("=" * 55)
    print(f"  {product_name} — Configuración Nginx Proxy Manager")
    print("=" * 55)

    existing = load_config() or {}

    def ask(msg, default=""):
        suffix = f" [{default}]" if default else ""
        val = input(f"{msg}{suffix}: ").strip()
        return val if val else default

    print("""
NPM corre típicamente en http://localhost:81
Si NPM está en Docker junto a la app usá su IP o nombre de servicio.
""")

    npm_url   = ask("URL de Nginx Proxy Manager", existing.get("npm_url", "http://localhost:81"))
    npm_email = ask("Email admin de NPM",         existing.get("npm_email", "admin@example.com"))
    npm_pass  = ask("Contraseña admin de NPM",    existing.get("npm_password", ""))

    print(f"""
forward_host es la IP/hostname al que NPM enruta el tráfico hacia los
contenedores {product_name}. Opciones comunes:
  172.17.0.1        → gateway Docker (NPM en contenedor, clientes en bridge)
  host.docker.internal → alternativa en algunos setups
  <IP LAN del servidor> → si NPM corre fuera de Docker
""")
    forward_host = ask("forward_host", existing.get("forward_host", "172.17.0.1"))

    le_email = ask("Email para Let's Encrypt (SSL)",
                   existing.get("le_email", npm_email))

    print("\n[*] Probando conexión a NPM ...")
    client = NPMClient(npm_url, npm_email, npm_pass)
    if not client.ping():
        print("[ERROR] No se pudo conectar. Verificá URL, email y contraseña.")
        sys.exit(1)
    print("[OK] Conexión exitosa.")

    save_config({
        "npm_url":      npm_url,
        "npm_email":    npm_email,
        "npm_password": npm_pass,
        "forward_host": forward_host,
        "le_email":     le_email,
    })
    print(f"[OK] Config guardada en {config_file()}")
    print("\nAhora podés usar el proxy automático al crear clientes con:")
    print("  python3 scripts/nuevo_cliente.py")
