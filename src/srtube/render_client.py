import json
from pathlib import Path

import aiohttp

from .errors import UserError


async def request(socket: Path, payload: dict, timeout: int) -> dict:
    connector = aiohttp.UnixConnector(path=str(socket))
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as client:
            async with client.post("http://renderer/request", json=payload) as response:
                raw = await response.content.read(32769)
                if response.status != 200 or len(raw) > 32768:
                    raise UserError("Не удалось обработать аудио. Попробуй позже.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                return result
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise UserError("Сервис обработки аудио временно недоступен.") from None
