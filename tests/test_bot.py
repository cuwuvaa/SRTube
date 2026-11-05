import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO

import pytest
import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageMedia,
    EditMessageText,
    GetFile,
    SendAudio,
    SendChatAction,
    SendMessage,
    SendPhoto,
    SendVoice,
)
from aiogram.types import Audio, CallbackQuery, Chat, Document, File, Message, PhotoSize, Update, User
from PIL import Image

from srtube.admin import AdminAction
from srtube.artwork import make_artwork
from srtube.bot import build_dispatcher
from srtube.config import Settings
from srtube.errors import UserError
from srtube.jobs import Jobs
from srtube.keyboards import Action, MetadataAction
from srtube.metadata_editor import MetadataInput, MP3Writer
from srtube.models import Effects, Track
from srtube.storage import Storage


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.uploads = []
        self.download = b""
        self.downloads = {}

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (AnswerCallbackQuery, SendChatAction)):
            return True
        if isinstance(method, GetFile):
            return File(
                file_id=method.file_id, file_unique_id=method.file_id, file_path=f"files/{method.file_id}"
            )
        audio = None
        photo = None
        if isinstance(method, (SendAudio, SendVoice)):
            file = method.audio if isinstance(method, SendAudio) else method.voice
            raw = b"".join([chunk async for chunk in file.read(bot)])
            self.uploads.append((type(method), raw))
            if isinstance(method, SendAudio):
                file_id = f"audio-{len(self.requests)}"
                self.downloads[file_id] = raw
                audio = Audio(
                    file_id=file_id,
                    file_unique_id=file_id,
                    duration=60,
                    title=method.title,
                    performer=method.performer,
                    file_size=len(raw),
                )
        if isinstance(method, (SendPhoto, EditMessageMedia)):
            file = method.photo if isinstance(method, SendPhoto) else method.media.media
            raw = b"".join([chunk async for chunk in file.read(bot)])
            self.uploads.append((type(method), raw))
            file_id = f"cover-{len(self.requests)}"
            self.downloads[file_id] = raw
            photo = [
                PhotoSize(file_id=file_id, file_unique_id=file_id, width=800, height=800, file_size=len(raw))
            ]
        return Message(
            message_id=len(self.requests) + 100,
            date=datetime.now(UTC),
            chat=Chat(id=int(method.chat_id), type="private"),
            text=method.text if isinstance(method, (SendMessage, EditMessageText)) else None,
            audio=audio,
            photo=photo,
        ).as_(bot)

    async def stream_content(self, *args, **kwargs):
        file_id = kwargs.get("url", "").rsplit("/", 1)[-1]
        yield self.downloads.get(file_id, self.download)


class FakeMedia:
    """Network boundary double; real DSP is exercised in test_media.py."""

    def __init__(self, storage):
        self.storage = storage
        self.rendered = []
        self.edited = []

    async def ingest(
        self, track_id, user_id, chat_id, url, progress, *, metadata_override=None, cover_raw=None
    ):
        await progress("Готовлю MP3…")
        directory = self.storage.directory(track_id)
        directory.mkdir()
        for name in ("source.webm", "original.mp3"):
            (directory / name).write_bytes(b"original")
        make_artwork(None, directory)
        track = Track(
            track_id, user_id, chat_id, "Song <3", "Artist & Co", url, 60, "source.webm", time.time()
        )
        if metadata_override:
            track = replace(metadata_override, source_name="source.webm")
        self.storage.save(track)
        return track

    async def render(self, track, effects, preview=False):
        self.rendered.append((effects, preview))
        path = self.storage.directory(track.id) / ("preview.ogg" if preview else "effects.mp3")
        path.write_bytes(b"preview" if preview else b"effects")
        return path

    async def ingest_mp3(
        self,
        track_id,
        user_id,
        chat_id,
        filename,
        download,
        progress,
        *,
        metadata_override=None,
        cover_raw=None,
    ):
        directory = self.storage.directory(track_id)
        directory.mkdir()
        await download(directory / "source.mp3")
        (directory / "original.mp3").write_bytes((directory / "source.mp3").read_bytes())
        make_artwork(None, directory)
        track = Track(
            track_id,
            user_id,
            chat_id,
            "Uploaded song",
            "Uploaded artist",
            "",
            60,
            "source.mp3",
            time.time(),
            album="Uploaded album",
        )
        if metadata_override:
            track = replace(metadata_override, source_name="source.mp3")
        self.storage.save(track)
        return track

    async def diagnose_youtube(self, url):
        return {"title": "Test <video>", "duration": 19}

    async def edit_metadata(self, track, *, raw=None, cover_action=None):
        self.edited.append((track, raw, cover_action))


@pytest_asyncio.fixture
async def bot_env(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, admin_user_ids="1")
    storage = Storage(tmp_path)
    media = FakeMedia(storage)
    session = RecordingSession()
    bot = Bot(
        "123456789:" + "A" * 35,
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = build_dispatcher(settings, storage, media, Jobs(2, 2))
    yield dp, bot, session, storage, media
    await dp.storage.close()
    await bot.session.close()
    storage.close()


def user_message(text, user_id=1):
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Test"),
        text=text,
    )


def mp3_message(kind="document", user_id=1, file_size=None):
    kwargs = dict(
        file_id="mp3-upload", file_unique_id="mp3-upload", file_name="track.MP3", file_size=file_size
    )
    file = Audio(**kwargs, duration=60, mime_type="audio/mpeg") if kind == "audio" else Document(**kwargs)
    return user_message(None, user_id).model_copy(update={kind: file})


def test_mp3_download_enforces_actual_bytes_when_telegram_size_is_missing(tmp_path):
    path = tmp_path / "source.mp3"
    with MP3Writer(path, limit=10) as stream:
        stream.write(b"1234567")
        with pytest.raises(UserError):
            stream.write(b"8901")
    assert path.read_bytes() == b"1234567"


async def click(dp, bot, track, action, effects=None, user_id=1):
    effects = effects or Effects()
    query = CallbackQuery(
        id="test-callback",
        chat_instance="test-chat",
        from_user=user_message("", user_id).from_user,
        message=user_message("panel", user_id),
        data=Action(action=action, track=track.id, slowed=effects.slowed, reverb=effects.reverb).pack(),
    )
    await dp.feed_update(bot, Update(update_id=2, callback_query=query))


async def metadata_click(dp, bot, track, action, user_id=1, photo=False):
    message = user_message("panel", user_id)
    if photo:
        message = message.model_copy(
            update={
                "text": None,
                "photo": [PhotoSize(file_id="cover", file_unique_id="cover", width=800, height=800)],
            }
        )
    query = CallbackQuery(
        id="metadata-callback",
        chat_instance="test-chat",
        from_user=user_message("", user_id).from_user,
        message=message,
        data=MetadataAction(action=action, track=track.id).pack(),
    )
    await dp.feed_update(bot, Update(update_id=20, callback_query=query))


async def test_metadata_editor_restores_expired_track_and_saved_edits_from_telegram(bot_env, monkeypatch):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    updated = replace(track, title="Сохранённое название", artist="Исполнитель", album="Альбом")
    storage.save(updated)
    assert storage.file(track.id, 1, "audio") is not None
    storage.evict(track.id)
    assert storage.record(track.id, 1) == updated

    async def no_youtube(*args, **kwargs):
        raise AssertionError("Restore must download the existing Telegram MP3")

    monkeypatch.setattr(media, "ingest", no_youtube)
    await metadata_click(dp, bot, updated, "open")
    restored = storage.get(track.id, 1)
    assert restored.title == updated.title and restored.album == updated.album
    assert restored.url == updated.url and restored.source_name == "source.mp3"
    assert "Сохранённое название" in session.requests[-1].text
    assert (storage.directory(track.id) / "original.mp3").read_bytes() == b"original"


async def test_uploaded_mp3_and_last_restore_after_eviction_without_changing_track_id(bot_env):
    dp, bot, session, storage, _ = bot_env
    session.download = b"own mp3"
    await dp.feed_update(bot, Update(update_id=1, message=mp3_message()))
    track = storage.latest(1)
    storage.evict(track.id)
    await dp.feed_update(bot, Update(update_id=2, message=user_message("/last")))
    assert session.uploads[-1] == (SendAudio, b"own mp3")
    assert storage.latest(1).id == track.id


async def test_edited_cover_is_saved_as_telegram_reference_for_restore(bot_env):
    dp, bot, _, storage, _ = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    await metadata_click(dp, bot, track, "rotate", photo=True)
    assert storage.file(track.id, 1, "cover") is not None
    storage.evict(track.id)
    await metadata_click(dp, bot, track, "open")
    assert storage.get(track.id, 1) is not None


async def test_large_telegram_mp3_uses_youtube_fallback_preserving_metadata(bot_env):
    dp, bot, session, storage, _ = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    updated = replace(track, title="Отредактированный трек", album="Мой альбом")
    storage.save(updated)
    storage.remember_file(track.id, 1, "audio", "large-audio", 30_000_000)
    storage.evict(track.id)
    await metadata_click(dp, bot, updated, "open")
    assert storage.get(track.id, 1).album == updated.album
    assert storage.get(track.id, 1).title == updated.title
    assert not any(
        isinstance(request, GetFile) and request.file_id == "large-audio" for request in session.requests
    )


async def test_album_can_be_set_and_cleared(bot_env):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    await metadata_click(dp, bot, track, "album")
    await dp.feed_update(bot, Update(update_id=3, message=user_message(" Альбом <3 & Co ")))
    assert storage.get(track.id, 1).album == "Альбом <3 & Co"
    assert "Альбом &lt;3 &amp; Co" in session.requests[-1].text
    await click(dp, bot, track, "original")
    audio = next(request for request in reversed(session.requests) if isinstance(request, SendAudio))
    assert "Альбом &lt;3 &amp; Co" in audio.caption
    await metadata_click(dp, bot, track, "clear_album")
    assert storage.get(track.id, 1).album == ""
    assert media.edited[-1][0].album == ""


@pytest.mark.parametrize("kind", ["audio", "document"])
async def test_received_mp3_opens_own_editor_and_returns_original_quality(bot_env, kind):
    dp, bot, session, storage, media = bot_env
    session.download = b"uploaded mp3 bytes"
    await dp.feed_update(bot, Update(update_id=1, message=mp3_message(kind)))
    track = storage.latest(1)
    assert track is not None and track.url == "" and track.source_name == "source.mp3"
    assert not session.uploads
    panel = session.requests[-1]
    assert isinstance(panel, EditMessageText)
    assert "Uploaded song" in panel.text and "Uploaded album" in panel.text
    assert any("Альбом" in button.text for row in panel.reply_markup.inline_keyboard for button in row)
    await metadata_click(dp, bot, track, "album")
    await dp.feed_update(bot, Update(update_id=3, message=user_message("Edited album")))
    assert storage.get(track.id, 1).album == "Edited album"
    await click(dp, bot, track, "original")
    assert session.uploads[-1] == (SendAudio, session.download)
    audio = next(request for request in reversed(session.requests) if isinstance(request, SendAudio))
    assert "исходное качество" in audio.caption and "320" not in audio.caption
    assert "Edited album" in audio.caption
    assert not media.rendered


@pytest.mark.parametrize("pending", ["title", "upload"])
async def test_incoming_mp3_starts_new_editor_and_leaves_previous_track_unchanged(bot_env, pending):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    previous = storage.latest(1)
    await metadata_click(dp, bot, previous, pending)
    session.download = b"new mp3"
    await dp.feed_update(bot, Update(update_id=3, message=mp3_message()))
    latest = storage.latest(1)
    assert latest.id != previous.id
    assert storage.get(previous.id, 1) == previous
    assert not media.edited
    assert await dp.fsm.get_context(bot=bot, chat_id=1, user_id=1).get_state() is None


async def test_mp3_upload_rejects_oversized_file_before_download_and_checks_access(bot_env):
    dp, bot, session, storage, _ = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=mp3_message(file_size=20_000_001)))
    assert "20 МБ" in session.requests[-1].text
    assert not any(isinstance(request, GetFile) for request in session.requests)
    assert storage.latest(1) is None
    await dp.feed_update(bot, Update(update_id=2, message=mp3_message(user_id=2)))
    assert "только приглашённым" in session.requests[-1].text
    assert storage.latest(2) is None


async def test_different_uploaded_files_keep_separate_editors(bot_env):
    dp, bot, session, storage, _ = bot_env
    session.download = b"first mp3"
    await dp.feed_update(bot, Update(update_id=1, message=mp3_message()))
    first = storage.latest(1)
    session.download = b"second mp3"
    await dp.feed_update(bot, Update(update_id=2, message=mp3_message()))
    second = storage.latest(1)
    assert first.id != second.id
    await metadata_click(dp, bot, first, "title")
    await dp.feed_update(bot, Update(update_id=3, message=user_message("First edited")))
    assert storage.get(second.id, 1).title == second.title
    await click(dp, bot, first, "original")
    assert session.uploads[-1] == (SendAudio, b"first mp3")
    await click(dp, bot, second, "original")
    assert session.uploads[-1] == (SendAudio, b"second mp3")


async def test_metadata_fields_persist_and_are_used_by_last_cache_and_effects(bot_env):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    audio = next(request for request in session.requests if isinstance(request, SendAudio))
    assert any("метаданные" in button.text for row in audio.reply_markup.inline_keyboard for button in row)
    await metadata_click(dp, bot, track, "open")
    assert "Artist &amp; Co" in session.requests[-1].text
    for field, value in (("artist", "Новый & Артист"), ("title", "  Новый <трек>  ")):
        await metadata_click(dp, bot, track, field)
        await dp.feed_update(bot, Update(update_id=3, message=user_message(value)))
    updated = storage.get(track.id, 1)
    assert updated.title == "Новый <трек>" and updated.artist == "Новый & Артист"
    assert "Новый &lt;трек&gt;" in session.requests[-1].text
    await dp.feed_update(bot, Update(update_id=4, message=user_message("/last")))
    sent = [request for request in session.requests if isinstance(request, SendAudio)][-1]
    assert sent.title == updated.title and sent.performer == updated.artist
    await click(dp, bot, track, "render", Effects(20, 0))
    sent = [request for request in session.requests if isinstance(request, SendAudio)][-1]
    assert sent.title == "Новый <трек> (slowed 20% + reverb 0%)"
    assert sent.performer == updated.artist
    assert storage.cached_url(track.url, 1) == updated
    assert not media.rendered[0][1]
    reopened = Storage(storage.root)
    try:
        assert reopened.get(track.id, 1) == updated
    finally:
        reopened.close()


@pytest.mark.parametrize("kind", ["photo", "document"])
async def test_upload_cover_and_edit_preview(bot_env, kind):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    await metadata_click(dp, bot, track, "upload")
    with BytesIO() as buffer:
        Image.new("RGB", (80, 60), "red").save(buffer, "PNG")
        session.download = buffer.getvalue()
    message = user_message(None)
    if kind == "photo":
        data = {"photo": [PhotoSize(file_id="upload", file_unique_id="upload", width=80, height=60)]}
    else:
        data = {"document": Document(file_id="upload", file_unique_id="upload", file_name="cover.png")}
    await dp.feed_update(bot, Update(update_id=2, message=message.model_copy(update=data)))
    assert media.edited[-1][1] == session.download
    assert session.uploads[-1][0] == SendPhoto
    await metadata_click(dp, bot, track, "rotate", photo=True)
    assert media.edited[-1][2] == "rotate"
    assert session.uploads[-1][0] == EditMessageMedia
    assert await dp.fsm.get_context(bot=bot, chat_id=1, user_id=1).get_state() is None


async def test_metadata_rejects_other_owner_expired_track_and_revoked_access(bot_env):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    storage.add_users({2}, 1)
    await metadata_click(dp, bot, track, "title", user_id=2)
    assert session.requests[-1].show_alert
    assert not media.edited
    await dp.feed_update(bot, Update(update_id=2, message=user_message("https://youtu.be/BaW_jenozKc", 2)))
    owned = storage.latest(2)
    await metadata_click(dp, bot, owned, "artist", user_id=2)
    storage.remove_users({2})
    await dp.feed_update(bot, Update(update_id=3, message=user_message("Forbidden", 2)))
    assert storage.get(owned.id, 2).artist == owned.artist
    assert not media.edited
    storage.remove(track.id)
    await metadata_click(dp, bot, track, "cover")
    assert session.requests[-1].show_alert and "кэше" in session.requests[-1].text


async def test_invalid_metadata_and_failed_cover_allow_retry(bot_env, monkeypatch):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    context = dp.fsm.get_context(bot=bot, chat_id=1, user_id=1)
    await metadata_click(dp, bot, track, "title")
    for value in ("  ", "a" * 181):
        await dp.feed_update(bot, Update(update_id=2, message=user_message(value)))
    assert not media.edited and storage.get(track.id, 1) == track
    assert await context.get_state() == MetadataInput.text.state
    await metadata_click(dp, bot, track, "upload")

    async def invalid_image(*args, **kwargs):
        raise UserError("Некорректное изображение")

    monkeypatch.setattr(media, "edit_metadata", invalid_image)
    session.download = b"invalid image"
    message = user_message(None).model_copy(
        update={"document": Document(file_id="upload", file_unique_id="upload", file_name="cover.png")}
    )
    await dp.feed_update(bot, Update(update_id=3, message=message))
    assert "Некорректное изображение" in session.requests[-1].text
    assert await context.get_state() == MetadataInput.cover.state
    assert storage.get(track.id, 1) == track


async def test_cancel_and_new_url_exit_metadata_input(bot_env):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    await metadata_click(dp, bot, track, "title")
    await dp.feed_update(bot, Update(update_id=2, message=user_message("/cancel")))
    assert await dp.fsm.get_context(bot=bot, chat_id=1, user_id=1).get_state() is None
    await metadata_click(dp, bot, track, "upload")
    await dp.feed_update(bot, Update(update_id=3, message=user_message("https://youtu.be/jNQXAC9IVRw")))
    assert storage.latest(1).id != track.id
    assert not media.edited


async def test_metadata_edit_blocks_duplicate_job(bot_env, monkeypatch):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def delayed_edit(*args, **kwargs):
        started.set()
        await finish.wait()

    monkeypatch.setattr(media, "edit_metadata", delayed_edit)
    await metadata_click(dp, bot, track, "title")
    task = asyncio.create_task(dp.feed_update(bot, Update(update_id=3, message=user_message("Updated"))))
    await asyncio.wait_for(started.wait(), timeout=2)
    await click(dp, bot, track, "render")
    assert "уже обрабатывается" in session.requests[-1].text
    finish.set()
    await task
    assert storage.get(track.id, 1).title == "Updated"


async def test_shutdown_finishes_metadata_publication_before_closing_storage(bot_env, monkeypatch):
    dp, bot, _, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    started = asyncio.Event()
    finish = asyncio.Event()

    async def delayed_edit(*args, **kwargs):
        started.set()
        await finish.wait()

    monkeypatch.setattr(media, "edit_metadata", delayed_edit)
    await metadata_click(dp, bot, track, "title")
    task = asyncio.create_task(
        dp.feed_update(bot, Update(update_id=3, message=user_message("Saved on shutdown")))
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    shutdown = asyncio.create_task(dp.emit_shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    finish.set()
    await asyncio.wait_for(shutdown, timeout=2)
    assert task.cancelled()
    assert storage.get(track.id, 1).title == "Saved on shutdown"


async def test_full_bot_flow_with_custom_percentages_and_voice(bot_env):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    assert track is not None
    assert session.uploads[-1] == (SendAudio, b"original")
    await click(dp, bot, track, "open")
    panel = [r for r in session.requests if isinstance(r, SendMessage)][-1]
    assert "Song &lt;3" in panel.text and "Artist &amp; Co" in panel.text
    await click(dp, bot, track, "custom")
    await dp.feed_update(bot, Update(update_id=3, message=user_message("17 35")))
    panel = [r for r in session.requests if isinstance(r, SendMessage)][-1]
    assert "<b>17%</b>" in panel.text and "<b>35%</b>" in panel.text
    effects = Effects(17, 35)
    await click(dp, bot, track, "preview", effects)
    assert session.uploads[-1] == (SendVoice, b"preview")
    assert not (storage.directory(track.id) / "preview.ogg").exists()
    await click(dp, bot, track, "render", effects)
    assert session.uploads[-1] == (SendAudio, b"effects")
    assert media.rendered == [(effects, True), (effects, False)]
    assert not (storage.directory(track.id) / "effects.mp3").exists()


async def test_user_cannot_process_someone_elses_track(bot_env):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    storage.add_users({2}, 1)
    before = len(session.uploads)
    await click(dp, bot, track, "render", user_id=2)
    assert len(session.uploads) == before
    assert not media.rendered
    assert isinstance(session.requests[-1], AnswerCallbackQuery)
    assert session.requests[-1].show_alert


async def test_new_url_exits_pending_custom_input(bot_env):
    dp, bot, session, storage, _ = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    await click(dp, bot, track, "custom")
    await dp.feed_update(bot, Update(update_id=3, message=user_message("https://youtu.be/jNQXAC9IVRw")))
    assert len([upload for upload in session.uploads if upload[0] == SendAudio]) == 2
    assert storage.latest(1).id != track.id


async def test_repeated_link_reuses_ready_mp3_without_requesting_youtube(bot_env, monkeypatch):
    dp, bot, session, storage, media = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)

    async def should_not_download(*args):
        raise AssertionError("Ready audio must be served from cache")

    monkeypatch.setattr(media, "ingest", should_not_download)
    await dp.feed_update(
        bot, Update(update_id=2, message=user_message("https://youtube.com/watch?v=BaW_jenozKc&t=5"))
    )
    assert len(session.uploads) == 2
    assert storage.latest(1).id == track.id


async def test_last_and_expired_track_callback(bot_env):
    dp, bot, session, storage, _ = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    track = storage.latest(1)
    await dp.feed_update(bot, Update(update_id=2, message=user_message("/last")))
    assert len(session.uploads) == 2
    storage.remove(track.id)
    await click(dp, bot, track, "open")
    assert isinstance(session.requests[-1], AnswerCallbackQuery)
    assert "кэше" in session.requests[-1].text


async def test_shutdown_cancels_inflight_update(bot_env, monkeypatch):
    dp, bot, _, _, media = bot_env
    started = asyncio.Event()

    async def blocked_ingest(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(media, "ingest", blocked_ingest)
    task = asyncio.create_task(
        dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc")))
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    await dp.emit_shutdown()
    assert task.cancelled()


async def admin_click(dp, bot, action, user_id=1, page=0):
    query = CallbackQuery(
        id="admin-callback",
        chat_instance="test-chat",
        from_user=user_message("", user_id).from_user,
        message=user_message("admin", user_id),
        data=AdminAction(action=action, page=page).pack(),
    )
    await dp.feed_update(bot, Update(update_id=10, callback_query=query))


async def test_closed_access_and_public_id(bot_env):
    dp, bot, session, storage, _ = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("https://youtu.be/BaW_jenozKc", 2)))
    assert not session.uploads and storage.latest(2) is None
    assert "только приглашённым" in session.requests[-1].text
    await dp.feed_update(bot, Update(update_id=2, message=user_message("/id", 2)))
    assert session.requests[-1].text == "Твой Telegram ID: <code>2</code>"


async def test_admin_can_add_and_revoke_user_immediately(bot_env):
    dp, bot, session, storage, _ = bot_env
    await dp.feed_update(bot, Update(update_id=1, message=user_message("/admin")))
    assert "Админка SRTube" in session.requests[-1].text
    await admin_click(dp, bot, "add")
    await dp.feed_update(bot, Update(update_id=2, message=user_message("2")))
    assert storage.is_allowed(2)
    await dp.feed_update(bot, Update(update_id=3, message=user_message("https://youtu.be/BaW_jenozKc", 2)))
    track = storage.latest(2)
    assert track is not None and session.uploads[-1][0] == SendAudio
    await admin_click(dp, bot, "remove")
    await dp.feed_update(bot, Update(update_id=4, message=user_message("2")))
    assert not storage.is_allowed(2)
    before = len(session.uploads)
    await click(dp, bot, track, "render", user_id=2)
    assert len(session.uploads) == before
    assert session.requests[-1].show_alert


async def test_allowed_user_cannot_forge_admin_buttons(bot_env):
    dp, bot, session, storage, _ = bot_env
    storage.add_users({2}, 1)
    await admin_click(dp, bot, "add", user_id=2)
    assert isinstance(session.requests[-1], AnswerCallbackQuery)
    assert session.requests[-1].show_alert
    await dp.feed_update(bot, Update(update_id=2, message=user_message("/admin", 2)))
    assert "только администратору" in session.requests[-1].text
    assert storage.user_count() == 1


async def test_admin_id_validation_and_self_removal_protection(bot_env):
    dp, bot, session, storage, _ = bot_env
    await admin_click(dp, bot, "add")
    await dp.feed_update(bot, Update(update_id=1, message=user_message("2 -3")))
    assert storage.user_count() == 0
    await dp.feed_update(bot, Update(update_id=2, message=user_message("2, 3\n4")))
    assert storage.user_count() == 3
    await admin_click(dp, bot, "remove")
    await dp.feed_update(bot, Update(update_id=3, message=user_message("1 2")))
    assert storage.is_allowed(2)
    assert "ADMIN_USER_IDS" in session.requests[-1].text
    await dp.feed_update(bot, Update(update_id=4, message=user_message("/cancel")))
    await dp.feed_update(bot, Update(update_id=5, message=user_message("/admin")))
    assert "Админка SRTube" in session.requests[-1].text


async def test_admin_list_paginates_and_youtube_status_hides_proxy_secrets(bot_env):
    dp, bot, session, storage, _ = bot_env
    storage.add_users(set(range(10, 36)), 1)
    await admin_click(dp, bot, "users", page=2)
    page = session.requests[-1]
    assert "<code>30</code>" in page.text and "<code>35</code>" in page.text
    assert "<code>10</code>" not in page.text
    await admin_click(dp, bot, "youtube")
    assert "Сессия не подключена" in session.requests[-1].text
    assert "test-session" not in session.requests[-1].text


async def test_admin_youtube_check_reports_result(bot_env):
    dp, bot, session, _, _ = bot_env
    await admin_click(dp, bot, "check")
    assert isinstance(session.requests[-1], EditMessageText)
    assert "✅ YouTube" in session.requests[-1].text
    assert "Test &lt;video&gt;" in session.requests[-1].text
