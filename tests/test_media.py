import array
import json
import math
import os
import shutil
import sys
import time
import wave
from dataclasses import replace
from io import BytesIO

import pytest
import pytest_asyncio
from mutagen.id3 import ID3
from mutagen.mp3 import MP3
from PIL import Image

from srtube.artwork import make_artwork
from srtube.config import Settings
from srtube.errors import UserError
from srtube.media import Media
from srtube.models import Effects, Track
from srtube.process import run_process
from srtube.storage import Storage

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")


@pytest_asyncio.fixture
async def audio(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, preview_seconds=5)
    settings.prepare()
    storage = Storage(tmp_path)
    media = Media(settings, storage)
    await media.check_tools()
    track = Track(
        "123456789abc", 1, 1, "Трек", "Артист", "https://youtu.be/BaW_jenozKc", 12, "source.wav", time.time()
    )
    directory = storage.directory(track.id)
    directory.mkdir()
    # Three sections let us verify that the preview really comes from the middle.
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        r"aevalsrc=0.25*sin(2*PI*if(lt(t\,4)\,220\,if(lt(t\,8)\,440\,880))*t):s=48000:d=12",
        "-ac",
        "2",
        str(directory / track.source_name),
        timeout=15,
    )
    make_artwork(None, directory)
    yield media, storage, track
    storage.close()


async def test_original_320k_and_embedded_unicode_tags_and_cover(audio):
    media, storage, track = audio
    path = await media.render(track)
    audio_file = MP3(path)
    assert audio_file.info.bitrate == 320000
    assert abs(audio_file.info.length - 12) < 0.1
    tags = ID3(path)
    assert tags["TIT2"].text == ["Трек"]
    assert tags["TPE1"].text == ["Артист"]
    cover = tags.getall("APIC")[0]
    assert cover.mime == "image/jpeg" and cover.type == 3
    assert Image.open(BytesIO(cover.data)).size == (800, 800)
    assert (storage.directory(track.id) / "thumbnail.jpg").stat().st_size < 200000


async def test_slowed_duration_and_middle_voice_pitch(audio):
    media, _, track = audio
    effects = Effects(20, 0)
    path = await media.render(track, effects)
    assert abs(MP3(path).info.length - 15) < 0.1
    assert "slowed 20% + reverb 0%" in ID3(path)["TIT2"].text[0]
    preview = await media.render(track, effects, preview=True)
    details = await media.probe(preview)
    assert details["streams"][0]["codec_name"] == "opus"
    assert 4.9 < float(details["format"]["duration"]) < 5.1
    raw = await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        "1",
        "-i",
        str(preview),
        "-t",
        "1",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-f",
        "f32le",
        "pipe:1",
        timeout=10,
    )
    samples = array.array("f", raw)
    crossings = sum(a < 0 <= b for a, b in zip(samples, samples[1:], strict=False))
    assert abs(crossings - 440 * effects.speed) < 3


@pytest.mark.parametrize("effects", [Effects(15, 25), Effects(50, 100), Effects(0, 0)])
async def test_render_effect_extremes_and_preview(audio, effects):
    media, _, track = audio
    path = await media.render(track, effects)
    info = MP3(path).info
    expected = 12 / effects.speed + (2.4 if effects.reverb else 0)
    assert abs(info.length - expected) < 0.15
    assert info.bitrate == 320000
    preview = await media.render(track, effects, preview=True)
    assert float((await media.probe(preview))["format"]["duration"]) < 5.1


async def test_reverb_has_audible_tail(audio):
    media, storage, track = audio
    path = storage.directory(track.id) / "source.wav"
    samples = array.array("h", [0] * 48000 * 2)
    samples[4800 * 2] = samples[4800 * 2 + 1] = 24000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(samples.tobytes())
    rendered = await media.render(replace(track, duration=1), Effects(0, 100))
    raw = await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        "0.25",
        "-i",
        str(rendered),
        "-t",
        "0.5",
        "-ac",
        "1",
        "-f",
        "f32le",
        "pipe:1",
        timeout=10,
    )
    values = array.array("f", raw)
    assert math.sqrt(sum(x * x for x in values) / len(values)) > 0.0001


async def test_reject_oversized_full_mp3_before_encoding_but_allow_preview(audio):
    media, _, track = audio
    media.settings.max_upload_bytes = 1_000_000
    with pytest.raises(UserError, match="лимита Telegram"):
        await media.render(track, Effects(50, 100))
    assert (await media.render(track, Effects(50, 100), preview=True)).exists()


async def test_process_timeout_terminates_child(tmp_path):
    pid_file = tmp_path / "child.pid"
    with pytest.raises(UserError, match="слишком много времени"):
        await run_process(
            sys.executable,
            "-c",
            "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)",
            str(pid_file),
            timeout=0.3,
        )
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


async def test_download_rejects_live_before_network_transfer(audio, monkeypatch):
    media, storage, track = audio
    calls = []

    async def fake_process(*args, **kwargs):
        calls.append(args)
        return json.dumps({"id": "BaW_jenozKc", "is_live": True, "duration": 60}).encode()

    monkeypatch.setattr("srtube.media.run_process", fake_process)
    with pytest.raises(UserError, match="прямые эфиры"):
        await media.download(track.url, storage.directory(track.id))
    assert len(calls) == 1


@pytest.mark.parametrize("availability", ["private", "premium_only", "subscriber_only"])
async def test_account_only_material_is_not_shared(audio, monkeypatch, availability):
    media, storage, track = audio

    async def fake_process(*args, **kwargs):
        return json.dumps({"id": "jNQXAC9IVRw", "duration": 12, "availability": availability}).encode()

    monkeypatch.setattr("srtube.media.run_process", fake_process)
    with pytest.raises(UserError, match="приватные"):
        await media.download(track.url, storage.directory(track.id))


async def test_download_uses_private_cookie_copy_and_removes_it(audio, monkeypatch, tmp_path):
    media, storage, track = audio
    original = tmp_path / "private-cookies.txt"
    cookies = b"# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\ttest-session\n"
    original.write_bytes(cookies)
    original.chmod(0o400)
    media.settings.youtube_cookies_file = original
    cookie_copies = []

    async def fake_process(*args, **kwargs):
        from pathlib import Path

        cookie_copy = Path(args[args.index("--cookies") + 1])
        cookie_copies.append(cookie_copy)
        assert cookie_copy != original
        assert cookie_copy.stat().st_mode & 0o777 == 0o600
        if "--dump-single-json" in args:
            assert cookie_copy.read_bytes() == cookies
            cookie_copy.write_bytes(b"yt-dlp changes")
            return json.dumps({"id": "BaW_jenozKc", "duration": 12}).encode()
        assert cookie_copy.read_bytes() == b"yt-dlp changes"
        return str(storage.directory(track.id) / track.source_name).encode()

    monkeypatch.setattr("srtube.media.run_process", fake_process)
    _, source = await media.download(track.url, storage.directory(track.id))
    assert source.is_file()
    assert original.read_bytes() == cookies
    assert all(not path.exists() for path in cookie_copies)
