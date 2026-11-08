from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split("  #")[0].strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    tenant_name: str
    app_id: int
    timezone: str
    beid: str
    web_services_key: str
    tech_password: str
    token_secret: str
    token_ttl_hours: int
    webhook_url: str
    db_path: Path
    host: str
    port: int


def load(env_file: Path | None = None) -> Settings:
    _load_dotenv(env_file or ROOT / ".env")
    e = os.environ.get
    return Settings(
        tenant_name=e("TENANT_NAME", "Harbor & Main Bank"),
        app_id=int(e("TICKET_APP_ID", "431")),
        timezone=e("TIMEZONE", "America/Los_Angeles"),
        beid=e("TDX_BEID", "7f2c1a44-6c3e-4b7b-9a1e-2d5f8c0b1e21"),
        web_services_key=e("TDX_WEB_SERVICES_KEY", ""),
        tech_password=e("TDX_TECH_PASSWORD", ""),
        token_secret=e("TDX_TOKEN_SECRET", "") or secrets.token_hex(32),
        token_ttl_hours=int(e("TOKEN_TTL_HOURS", "24")),
        webhook_url=e("WEBHOOK_URL", ""),
        db_path=Path(e("DB_PATH", str(ROOT / "data" / "mocktdx.db"))),
        host=e("HOST", "127.0.0.1"),
        port=int(e("PORT", "8090")),
    )
