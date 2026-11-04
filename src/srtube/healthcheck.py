"""Cheap stdlib-only Docker health checks; no network request to Telegram/YouTube."""

import json
import os
import socket
import sys
import time
from pathlib import Path


def check(directory: Path) -> bool:
    try:
        path = directory / "health.json"
        if path.stat().st_size > 4096:
            return False
        data = json.loads(path.read_text())
        os.kill(data["pid"], 0)
        return 0 <= time.time() - data["at"] < 90 and data["revision"] == os.environ.get(
            "SRTUBE_REVISION", "local"
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def renderer_check() -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(os.environ.get("RENDERER_SOCKET_PATH", "/run/srtube/renderer.sock"))
            connection.sendall(b"GET /health HTTP/1.0\r\nHost: renderer\r\n\r\n")
            return b" 200 " in connection.recv(128).split(b"\r\n", 1)[0]
    except OSError:
        return False


if __name__ == "__main__":
    good = False
    if sys.argv[1:] == ["bot"]:
        good = check(Path(os.environ.get("DATA_DIR", "data")))
    elif sys.argv[1:] == ["renderer"]:
        good = renderer_check()
    raise SystemExit(0 if good else 1)
