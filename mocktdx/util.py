from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone


def now_iso() -> str:
    """TDX returns UTC timestamps like 2026-11-03T19:48:21.123Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def sign_token(secret: str, payload: dict, ttl_seconds: int) -> str:
    body = dict(payload, exp=int(time.time()) + ttl_seconds)
    raw = base64.urlsafe_b64encode(json.dumps(body, separators=(",", ":")).encode()).rstrip(b"=")
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).rstrip(b"=")
    return f"{raw.decode()}.{sig.decode()}"


def verify_token(secret: str, token: str) -> dict | None:
    try:
        raw, sig = token.split(".", 1)
        expected = base64.urlsafe_b64encode(hmac.new(secret.encode(), raw.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(sig, expected):
            return None
        body = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if body.get("exp", 0) < time.time():
            return None
        return body
    except Exception:
        return None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        extra = getattr(record, "extra", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel("WARNING")
