"""
Cliente para la API REST de Nginx Proxy Manager (NPM).
Documentación NPM API: http://<npm-host>:81/api/

La ruta del archivo de config (antes `scripts/.npm_config.json`, derivada
de `__file__` dentro de cada producto) ahora se inyecta vía `configure()` —
mismo patrón que el resto de LibraCore. Cada producto llama `configure()`
una sola vez, al principio de su shim `scripts/npm_api.py`.
"""
import json
import threading
from pathlib import Path

import httpx


class NPMError(Exception):
    pass


class NPMClient:
    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.email    = email
        self.password = password
        self._token: str | None = None

    # ── auth ──────────────────────────────────────────────────────────────────

    def _authenticate(self) -> str:
        r = httpx.post(
            f"{self.base_url}/api/tokens",
            json={"identity": self.email, "secret": self.password},
            timeout=10,
        )
        if r.status_code != 200:
            raise NPMError(f"Autenticación fallida ({r.status_code}): {r.text}")
        self._token = r.json()["token"]
        return self._token

    def _headers(self) -> dict:
        if not self._token:
            self._authenticate()
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path: str) -> dict | list:
        r = httpx.get(f"{self.base_url}{path}", headers=self._headers(), timeout=15)
        if r.status_code == 401:
            self._authenticate()
            r = httpx.get(f"{self.base_url}{path}", headers=self._headers(), timeout=15)
        if not r.is_success:
            raise NPMError(f"GET {path} → {r.status_code}: {r.text}")
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = httpx.post(f"{self.base_url}{path}", json=body,
                       headers=self._headers(), timeout=30)
        if r.status_code == 401:
            self._authenticate()
            r = httpx.post(f"{self.base_url}{path}", json=body,
                           headers=self._headers(), timeout=30)
        if not r.is_success:
            raise NPMError(f"POST {path} → {r.status_code}: {r.text}")
        return r.json()

    def _put(self, path: str, body: dict) -> dict:
        r = httpx.put(f"{self.base_url}{path}", json=body,
                      headers=self._headers(), timeout=30)
        if not r.is_success:
            raise NPMError(f"PUT {path} → {r.status_code}: {r.text}")
        return r.json()

    def _delete(self, path: str) -> bool:
        r = httpx.delete(f"{self.base_url}{path}", headers=self._headers(), timeout=15)
        return r.is_success

    # ── proxy hosts ───────────────────────────────────────────────────────────

    def list_proxy_hosts(self) -> list[dict]:
        return self._get("/api/nginx/proxy-hosts")  # type: ignore

    def get_proxy_host_by_domain(self, domain: str) -> dict | None:
        for h in self.list_proxy_hosts():
            if domain in h.get("domain_names", []):
                return h
        return None

    def create_proxy_host(
        self,
        domain: str,
        forward_host: str,
        forward_port: int,
        ssl: bool = True,
        force_ssl: bool = True,
        http2: bool = True,
        websocket: bool = True,
        le_email: str = "",
    ) -> dict:
        """
        Crea un proxy host en NPM.
        Con ssl=True solicita un certificado Let's Encrypt automáticamente.
        """
        body: dict = {
            "domain_names":          [domain],
            "forward_scheme":        "http",
            "forward_host":          forward_host,
            "forward_port":          forward_port,
            "ssl_forced":            force_ssl if ssl else False,
            "http2_support":         http2 if ssl else False,
            "allow_websocket_upgrade": websocket,
            "block_exploits":        True,
            "caching_enabled":       False,
            "access_list_id":        0,
            "advanced_config":       "",
            "enabled":               True,
            "locations":             [],
            "meta": {
                "letsencrypt_agree": ssl,
                "dns_challenge":     False,
                "letsencrypt_email": le_email,
            },
        }
        if ssl:
            body["certificate_id"] = "new"
        return self._post("/api/nginx/proxy-hosts", body)

    def update_proxy_host(self, host_id: int, changes: dict) -> dict:
        return self._put(f"/api/nginx/proxy-hosts/{host_id}", changes)

    def delete_proxy_host(self, host_id: int) -> bool:
        return self._delete(f"/api/nginx/proxy-hosts/{host_id}")

    def enable_proxy_host(self, host_id: int) -> dict:
        return self._post(f"/api/nginx/proxy-hosts/{host_id}/enable", {})

    def disable_proxy_host(self, host_id: int) -> dict:
        return self._post(f"/api/nginx/proxy-hosts/{host_id}/disable", {})

    # ── certificates ─────────────────────────────────────────────────────────

    def list_certificates(self) -> list[dict]:
        return self._get("/api/nginx/certificates")  # type: ignore

    # ── ping ─────────────────────────────────────────────────────────────────

    def ping(self) -> bool:
        try:
            self._authenticate()
            return True
        except Exception:
            return False


# ── config helpers ────────────────────────────────────────────────────────────

_lock = threading.Lock()
_config_file: Path | None = None


def configure(config_file):
    """Fija la ruta del archivo de config (`scripts/.npm_config.json` de
    cada producto). Llamar una sola vez, al principio del script."""
    global _config_file
    with _lock:
        _config_file = Path(config_file)


def config_file() -> Path:
    if _config_file is None:
        raise RuntimeError(
            "libracore.npm_api no está configurado — llamar "
            "configure(config_file=...) al principio del script."
        )
    return _config_file


def load_config() -> dict | None:
    p = config_file()
    if not p.exists():
        return None
    return json.loads(p.read_text())


def save_config(cfg: dict):
    config_file().write_text(json.dumps(cfg, indent=2))


def client_from_config() -> "NPMClient | None":
    cfg = load_config()
    if not cfg:
        return None
    return NPMClient(cfg["npm_url"], cfg["npm_email"], cfg["npm_password"])


def forward_host_from_config() -> str:
    cfg = load_config() or {}
    return cfg.get("forward_host", "172.17.0.1")


def le_email_from_config() -> str:
    cfg = load_config() or {}
    return cfg.get("le_email", "")
