"""Unix-socket renderer. Production container has no network, bot token, DB, or cookies."""

import asyncio
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from aiohttp import web
from pydantic import SecretStr

from .artwork import MAX_ARTWORK_BYTES, make_artwork
from .config import Settings
from .errors import InvalidArtwork, InvalidMP3, MP3TooLong
from .media import Media
from .models import Effects, Track
from .security import SOURCE_NAME, media_file
from .storage import Storage


async def application(settings: Settings) -> web.Application:
    settings.prepare()
    storage = Storage(settings.data_dir, settings.tracks_dir)
    media = Media(settings, storage)
    await media.check_tools()
    gate = asyncio.Lock()
    app = web.Application(client_max_size=8192)

    async def handle(request: web.Request) -> web.Response:
        if gate.locked():
            return web.json_response({"error": "busy"}, status=429)
        async with gate:
            try:
                payload = await request.json()
                operation = payload["operation"]
                directory = storage.directory(payload["track_id"])
                if not directory.is_dir():
                    raise ValueError
                if operation == "probe":
                    name = payload["source_name"]
                    if not SOURCE_NAME.fullmatch(name):
                        raise ValueError
                    details = await media.probe(media_file(directory, name))
                    return web.json_response(
                        {
                            "format": {"duration": details["format"]["duration"]},
                            "streams": [
                                {"codec_name": stream.get("codec_name")} for stream in details["streams"][:4]
                            ],
                        }
                    )
                if operation == "artwork":
                    raw_path = media_file(directory, "artwork.input.jpg")
                    raw = (
                        raw_path.read_bytes()
                        if raw_path.is_file() and raw_path.stat().st_size <= 10_000_000
                        else None
                    )
                    try:
                        await asyncio.to_thread(make_artwork, raw, directory)
                    finally:
                        raw_path.unlink(missing_ok=True)
                    return web.json_response({"ok": True})
                if operation == "render":
                    track = Track(**payload["track"])
                    if track.id != directory.name:
                        raise ValueError
                    effects = Effects(**payload["effects"]) if payload.get("effects") else None
                    preview = payload.get("preview", False)
                    if type(preview) is not bool:
                        raise ValueError
                    path = await media.render(track, effects, preview)
                    return web.json_response({"file": path.name, "track": asdict(track)["id"]})
                if operation == "metadata":
                    track = Track(**payload["track"])
                    upload = payload.get("upload", False)
                    if track.id != directory.name or type(upload) is not bool:
                        raise ValueError
                    raw_path = media_file(directory, "artwork.upload")
                    try:
                        raw = None
                        if upload:
                            if not raw_path.is_file() or not 0 < raw_path.stat().st_size <= MAX_ARTWORK_BYTES:
                                raise ValueError
                            raw = raw_path.read_bytes()
                        await media.edit_metadata(track, raw=raw, cover_action=payload.get("cover_action"))
                    finally:
                        raw_path.unlink(missing_ok=True)
                    return web.json_response({"ok": True})
                if operation == "import_mp3":
                    track = await media.import_mp3(
                        directory.name,
                        payload["user_id"],
                        payload["chat_id"],
                        payload["filename"],
                        duration_limit=payload["duration_limit"],
                        byte_limit=payload["byte_limit"],
                    )
                    return web.json_response({"track": asdict(track)})
                raise ValueError
            except InvalidArtwork:
                return web.json_response({"error": "invalid artwork"}, status=422)
            except InvalidMP3:
                return web.json_response({"error": "invalid mp3"}, status=422)
            except MP3TooLong:
                return web.json_response({"error": "mp3 too long"}, status=422)
            except (KeyError, TypeError, ValueError):
                return web.json_response({"error": "invalid request"}, status=400)
            except Exception:
                return web.json_response({"error": "processing failed"}, status=422)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def close(app: web.Application) -> None:
        storage.close()

    app.router.add_post("/request", handle)
    app.router.add_get("/health", health)
    app.on_cleanup.append(close)
    return app


def main() -> None:
    os.umask(0o077)
    socket = Path(os.environ.get("RENDERER_SOCKET_PATH", "/run/srtube/renderer.sock"))
    socket.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="srtube-renderer-") as temporary:
        settings = Settings(
            _env_file=None,
            environment="legacy",
            bot_token=SecretStr(""),
            bot_token_file=None,
            renderer_socket=None,
            data_dir=Path(temporary),
            tracks_dir=Path(os.environ.get("TRACKS_DIR", "/app/tracks")),
            max_workers=1,
            min_free_bytes=50_000_000,
        )
        web.run_app(application(settings), path=str(socket), print=None, access_log=None)


if __name__ == "__main__":
    main()
