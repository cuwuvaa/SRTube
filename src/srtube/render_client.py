import json
from pathlib import Path

import aiohttp

from .errors import InvalidArtwork, InvalidMP3, MP3TooLong, UserError


async def request(socket: Path, payload: dict, timeout: int) -> dict:
    connector = aiohttp.UnixConnector(path=str(socket))
    try:
        async with aiohttp.ClientSession(
            connector=connector, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as client:
            async with client.post("http://renderer/request", json=payload) as response:
                raw = await response.content.read(32769)
                if len(raw) > 32768:
                    raise UserError("Не удалось обработать аудио. Попробуй позже.")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError
                if response.status != 200:
                    if payload.get("operation") == "metadata" and result.get("error") == "invalid artwork":
                        raise InvalidArtwork()
                    if payload.get("operation") == "import_mp3" and result.get("error") == "invalid mp3":
                        raise InvalidMP3()
                    if payload.get("operation") == "import_mp3" and result.get("error") == "mp3 too long":
                        raise MP3TooLong(payload["duration_limit"])
                    raise UserError("Не удалось обработать аудио. Попробуй позже.")
                return result
    except (aiohttp.ClientError, TimeoutError, ValueError):
        raise UserError("Сервис обработки аудио временно недоступен.") from None
