import time
import wave
from dataclasses import asdict

from aiohttp.test_utils import TestClient, TestServer

from srtube.config import Settings
from srtube.models import Effects, Track
from srtube.renderer import application
from srtube.security import track_directory


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
