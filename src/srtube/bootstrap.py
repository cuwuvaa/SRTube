"""One-time migration of private state through stdin, never through image layers or argv."""

import base64
import json
import os
import sqlite3
import sys
from pathlib import Path

from aiogram.utils.token import validate_token

from .youtube import import_cookies


def install(root: Path, raw: bytes) -> None:
    if len(raw) > 8_000_000:
        raise ValueError("State import too large")
    payload = json.loads(raw)
    token = payload["bot_token"]
    validate_token(token)
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    auth = root / "auth"
    auth.mkdir(mode=0o700, exist_ok=True)
    token_path = auth / "bot.token"
    if token_path.exists():
        raise ValueError("State already provisioned; refusing to overwrite credentials")
    token_path.write_text(token + "\n")
    token_path.chmod(0o600)
    if payload.get("cookies"):
        import_cookies(base64.b64decode(payload["cookies"], validate=True), auth / "youtube.cookies.txt")
    if payload.get("database"):
        database = root / "srtube.sqlite3"
        if database.exists():
            raise ValueError("Database already exists")
        database.write_bytes(base64.b64decode(payload["database"], validate=True))
        with sqlite3.connect(database) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Imported database is invalid")


if __name__ == "__main__":
    try:
        install(Path(os.environ.get("DATA_DIR", "/app/state")), sys.stdin.buffer.read(8_000_001))
        print("OK: private state installed")
    except Exception:
        print("State installation failed; no secret values were logged", file=sys.stderr)
        raise SystemExit(1) from None
