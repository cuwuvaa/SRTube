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
from mutagen.id3 import APIC, ID3, TALB, TCOM, TDOR, TIT2, TPE1, TXXX, USLT, WOAS
from mutagen.mp3 import MP3
from PIL import Image

from srtube.artwork import make_artwork
from srtube.config import Settings
from srtube.errors import InvalidMP3, UserError
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


async def test_metadata_edit_preserves_audio_frames_and_updates_embedded_tags_and_cover(audio):
    media, storage, track = audio
    path = await media.render(track)
    frames = path.read_bytes()[ID3(path).size :]
    source = storage.directory(track.id) / track.source_name
    source_bytes = source.read_bytes()
    updated = replace(track, title="Новое название <3", artist="Новый & исполнитель", album="Новый альбом")
    with BytesIO() as buffer:
        Image.new("RGB", (640, 480), "#e03020").save(buffer, "PNG")
        await media.edit_metadata(updated, raw=buffer.getvalue())
    tags = ID3(path)
    assert tags["TIT2"].text == [updated.title]
    assert tags["TPE1"].text == [updated.artist]
    assert tags["TALB"].text == [updated.album]
    assert tags["WOAS"].url == track.url
    assert tags.version == (2, 3, 0)
    assert path.read_bytes()[tags.size :] == frames
    assert source.read_bytes() == source_bytes
    directory = storage.directory(track.id)
    assert tags.getall("APIC")[0].data == (directory / "cover.jpg").read_bytes()
    with Image.open(directory / "cover.jpg") as cover:
        assert cover.size == (800, 800)
        assert cover.getpixel((400, 400))[0] > 200
    assert (directory / "thumbnail.jpg").stat().st_size < 200_000
    rendered = await media.render(updated, Effects(20, 0))
    assert ID3(rendered)["TPE1"].text == [updated.artist]
    assert ID3(rendered)["TALB"].text == [updated.album]
    assert ID3(rendered).getall("APIC")[0].data == (directory / "cover.jpg").read_bytes()
    await media.edit_metadata(replace(updated, album=""))
    assert "TALB" not in ID3(path)


@pytest.mark.parametrize("tag_version", [3, 4])
async def test_imported_mp3_preserves_audio_quality_other_tags_and_original_embedded_cover(
    audio, tmp_path, tag_version
):
    media, storage, original = audio
    incoming = tmp_path / "incoming.mp3"
    await run_process(
        "ffmpeg",
        "-v",
        "error",
        "-i",
        str(storage.directory(original.id) / original.source_name),
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(incoming),
        timeout=15,
    )
    with BytesIO() as buffer:
        Image.new("RGB", (120, 80), "#d03020").save(buffer, "PNG")
        embedded_cover = buffer.getvalue()
    tags = ID3(incoming)
    for frame in (
        TIT2(encoding=3, text="Загруженный трек"),
        TPE1(encoding=3, text="Исполнитель"),
        TALB(encoding=3, text="Исходный альбом"),
        TCOM(encoding=3, text="Композитор"),
        USLT(encoding=3, lang="rus", desc="Lyrics", text="Текст песни"),
        TXXX(encoding=3, desc="custom", text="extra metadata"),
        WOAS(url="https://example.com/source"),
        APIC(encoding=3, mime="image/png", type=3, desc="Original", data=embedded_cover),
    ):
        tags.add(frame)
    if tag_version == 4:
        tags.add(TDOR(encoding=3, text="2005-05-10"))
    tags.save(incoming, v2_version=tag_version)
    before = incoming.read_bytes()
    frames = before[ID3(incoming).size :]
    progress_messages = []

    async def download(path):
        shutil.copyfile(incoming, path)

    async def progress(text):
        progress_messages.append(text)

    track = await media.ingest_mp3("abcdef123456", 7, 7, "incoming.mp3", download, progress)
    assert (
        track.title == "Загруженный трек"
        and track.artist == "Исполнитель"
        and track.album == "Исходный альбом"
    )
    assert track.url == "" and track.source_name == "source.mp3"
    assert storage.get(track.id, 7) == track and storage.get(track.id, 8) is None
    directory = storage.directory(track.id)
    path = directory / "original.mp3"
    assert path.read_bytes() == before
    updated = replace(track, title="Исправленный трек", album="Исправленный альбом")
    await media.edit_metadata(updated)
    tags = ID3(path)
    assert path.read_bytes()[tags.size :] == frames
    assert MP3(path).info.bitrate == 128_000
    assert (directory / "source.mp3").read_bytes() == before
    assert tags["TIT2"].text == [updated.title] and tags["TALB"].text == [updated.album]
    assert tags["TCOM"].text == ["Композитор"]
    assert tags.getall("USLT")[0].text == "Текст песни"
    assert tags["TXXX:custom"].text == ["extra metadata"]
    assert tags["WOAS"].url == "https://example.com/source"
    assert tags.getall("APIC")[0].data == embedded_cover
    assert tags.version == (2, tag_version, 0)
    if tag_version == 4:
        assert str(tags["TDOR"].text[0]) == "2005-05-10"
    assert progress_messages
    await media.edit_metadata(updated, cover_action="rotate")
    assert ID3(path).getall("APIC")[0].data == (directory / "cover.jpg").read_bytes()


async def test_uploaded_mp3_without_id3_uses_filename_and_can_receive_tags(audio, tmp_path):
    media, storage, original = audio
    incoming = await media.render(original)
    ID3(incoming).delete(incoming, delete_v1=True, delete_v2=True)

    async def download(path):
        shutil.copyfile(incoming, path)

    async def progress(text):
        pass

    track = await media.ingest_mp3("abcdef123456", 1, 1, "../../Своя песня.MP3", download, progress)
    assert track.title == "Своя песня" and track.album == "" and track.artist == "Неизвестный исполнитель"
    updated = replace(track, album="Альбом")
    await media.edit_metadata(updated)
    tags = ID3(storage.directory(track.id) / "original.mp3")
    assert tags["TALB"].text == ["Альбом"]
    assert not tags.getall("APIC")  # A text edit preserves the absence of an embedded cover.


@pytest.mark.parametrize("failure", ["broken", "duration", "download"])
async def test_failed_mp3_import_removes_incomplete_files(audio, failure):
    media, storage, original = audio
    incoming = await media.render(original)
    if failure == "duration":
        media.settings.max_duration_seconds = 10

    async def download(path):
        if failure == "download":
            path.write_bytes(b"partial")
            raise UserError("Download interrupted")
        if failure == "broken":
            path.write_bytes(b"This is not an MP3")
        else:
            shutil.copyfile(incoming, path)

    async def progress(text):
        pass

    with pytest.raises(InvalidMP3 if failure == "broken" else UserError):
        await media.ingest_mp3("abcdef123456", 1, 1, "fake.mp3", download, progress)
    assert not storage.directory("abcdef123456").exists()
    assert storage.get("abcdef123456", 1) is None


async def test_text_edit_preserves_cover_and_failed_image_leaves_all_assets_intact(audio):
    media, storage, track = audio
    await media.render(track)
    directory = storage.directory(track.id)
    cover = (directory / "cover.jpg").read_bytes()
    await media.edit_metadata(replace(track, title="Новое название"))
    assert (directory / "cover.jpg").read_bytes() == cover
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(UserError, match="изображение"):
        await media.edit_metadata(track, raw=b"not an image")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


async def test_cover_edit_rolls_back_if_publication_fails(audio, monkeypatch):
    media, storage, track = audio
    await media.render(track)
    directory = storage.directory(track.id)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    original_replace = os.replace

    def fail_original_once(source, destination):
        if destination == directory / "original.mp3":
            raise OSError("Simulated disk failure")
        return original_replace(source, destination)

    monkeypatch.setattr("srtube.media.os.replace", fail_original_once)
    with pytest.raises(OSError, match="disk failure"):
        await media.edit_metadata(track, cover_action="rotate")
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


async def test_metadata_edit_checks_final_file_size_before_publishing(audio):
    media, storage, track = audio
    path = await media.render(track)
    before = path.read_bytes()
    media.settings.max_upload_bytes = len(before) - 1
    with pytest.raises(UserError):
        await media.edit_metadata(replace(track, title="Other title"))
    assert path.read_bytes() == before


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
