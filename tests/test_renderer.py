import tempfile
import time
import wave
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from mutagen.id3 import ID3
from PIL import Image

from srtube.config import Settings
from srtube.errors import InvalidArtwork, InvalidMP3, UserError
from srtube.media import Media
from srtube.models import Effects, Track
from srtube.render_client import request
from srtube.renderer import application
from srtube.security import track_directory
from srtube.storage import Storage


async def test_renderer_rejects_path_injection_and_generates_real_audio(tmp_path):
    state = tmp_path / "worker-private"
    tracks = tmp_path / "shared-tracks"
    settings = Settings(_env_file=None, data_dir=state, tracks_dir=tracks, min_free_bytes=50_000_000)
    app = await application(settings)
    async with TestClient(TestServer(app)) as client:
        response = await client.post("/request", json={"operation": "probe", "track_id": "../../state/auth"})
        assert response.status == 400
        track = Track(
            "123456789abc",
            1,
            1,
            "song",
            "artist",
            "https://youtu.be/jNQXAC9IVRw",
            1,
            "source.wav",
            time.time(),
        )
        directory = track_directory(tracks, track.id)
        directory.mkdir()
        with wave.open(str(directory / track.source_name), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\0\0" * 48000)
        response = await client.post(
            "/request", json={"operation": "probe", "track_id": track.id, "source_name": "../../secret"}
        )
        assert response.status == 400
        response = await client.post("/request", json={"operation": "artwork", "track_id": track.id})
        assert response.status == 200
        response = await client.post(
            "/request",
            json={
                "operation": "render",
                "track_id": track.id,
                "track": asdict(track),
                "effects": asdict(Effects()),
                "preview": False,
            },
        )
        assert response.status == 200 and (await response.json())["file"] == "effects.mp3"
        assert (directory / "effects.mp3").is_file()


async def test_metadata_and_cover_edit_through_real_unix_socket(tmp_path):
    tracks = tmp_path / "shared-tracks"
    worker_settings = Settings(
        _env_file=None, data_dir=tmp_path / "worker", tracks_dir=tracks, min_free_bytes=50_000_000
    )
    runner = web.AppRunner(await application(worker_settings))
    await runner.setup()
    try:
        with tempfile.TemporaryDirectory(prefix="srtube-socket-", dir="/tmp") as temporary:
            socket = Path(temporary) / "renderer.sock"
            await web.UnixSite(runner, str(socket)).start()
            settings = Settings(
                _env_file=None,
                data_dir=tmp_path / "bot",
                tracks_dir=tracks,
                renderer_socket=socket,
                min_free_bytes=50_000_000,
            )
            settings.prepare()
            storage = Storage(settings.data_dir, tracks)
            try:
                media = Media(settings, storage)
                track = Track(
                    "123456789abc",
                    1,
                    1,
                    "Song",
                    "Artist",
                    "https://youtu.be/jNQXAC9IVRw",
                    1,
                    "source.wav",
                    time.time(),
                )
                directory = storage.directory(track.id)
                directory.mkdir()
                with wave.open(str(directory / track.source_name), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(48000)
                    output.writeframes(b"\0\0" * 48000)
                # The worker creates artwork; the bot never needs to decode an image.
                await request(socket, {"operation": "artwork", "track_id": track.id}, 60)
                path = await media.render(track)
                frames = path.read_bytes()[ID3(path).size :]
                updated = replace(track, artist="Артист & Co", title="Новый <трек>")
                with BytesIO() as buffer:
                    Image.new("RGB", (160, 100), "#d05020").save(buffer, "PNG")
                    await media.edit_metadata(updated, raw=buffer.getvalue())
                await media.edit_metadata(updated, cover_action="rotate")
                tags = ID3(path)
                assert tags["TIT2"].text == [updated.title]
                assert tags["TPE1"].text == [updated.artist]
                assert tags.getall("APIC")[0].data == (directory / "cover.jpg").read_bytes()
                assert path.read_bytes()[tags.size :] == frames
                assert not (directory / "artwork.upload").exists()
                before = path.read_bytes()
                with pytest.raises(InvalidArtwork, match="JPEG, PNG"):
                    await media.edit_metadata(updated, raw=b"bad upload")
                assert path.read_bytes() == before
                imported_directory = storage.directory("abcdef123456")
                imported_directory.mkdir()
                (imported_directory / "source.mp3").write_bytes(before)
                imported = await media.import_mp3("abcdef123456", 7, 7, "incoming.mp3")
                assert imported.user_id == 7 and imported.title == updated.title and imported.url == ""
                assert (imported_directory / "original.mp3").read_bytes() == before
                await media.edit_metadata(replace(imported, album="Альбом из MP3"))
                assert ID3(imported_directory / "original.mp3")["TALB"].text == ["Альбом из MP3"]
                broken_directory = storage.directory("bbbbbbbbbbbb")
                broken_directory.mkdir()
                (broken_directory / "source.mp3").write_bytes(b"broken MP3")
                with pytest.raises(InvalidMP3, match="MP3"):
                    await media.import_mp3("bbbbbbbbbbbb", 7, 7, "fake.mp3")
                assert not (broken_directory / "original.mp3").exists()
                assert not (directory / "artwork.upload").exists()
                with pytest.raises(UserError):
                    await request(
                        socket,
                        {
                            "operation": "metadata",
                            "track_id": track.id,
                            "track": asdict(replace(track, id="a" * 12)),
                        },
                        60,
                    )
                assert path.read_bytes() == before
            finally:
                storage.close()
    finally:
        await runner.cleanup()
