import asyncio
import json
import os
import sys
import time
from pathlib import Path

import aiohttp
from aiogram.client.session.middlewares.base import BaseRequestMiddleware


class Health(BaseRequestMiddleware):
    def __init__(self, directory: Path):
        self.path = directory / "health.json"
        self.last_write = 0.0
        self.path.unlink(missing_ok=True)

    def touch(self) -> None:
        if time.monotonic() - self.last_write < 5:
            return
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "at": time.time(),
                    "pid": os.getpid(),
                    "revision": os.environ.get("SRTUBE_REVISION", "local"),
                }
            )
        )
        temporary.replace(self.path)
        self.last_write = time.monotonic()

    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if method.__api_method__ == "getUpdates":
            self.touch()
        return result


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


async def renderer_check() -> bool:
    socket = os.environ.get("RENDERER_SOCKET_PATH", "/run/srtube/renderer.sock")
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=socket), timeout=aiohttp.ClientTimeout(total=3)
        ) as client:
            async with client.get("http://renderer/health") as response:
                return response.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False


if __name__ == "__main__":
    raise SystemExit(
        0 if len(sys.argv) == 2 and sys.argv[1] == "renderer" and asyncio.run(renderer_check()) else 1
    )
