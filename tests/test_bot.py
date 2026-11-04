import asyncio
import time
from datetime import UTC, datetime

import pytest_asyncio
from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendAudio,
    SendChatAction,
    SendMessage,
    SendVoice,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from srtube.admin import AdminAction
from srtube.bot import build_dispatcher
from srtube.config import Settings
from srtube.jobs import Jobs
from srtube.keyboards import Action
from srtube.models import Effects, Track
from srtube.storage import Storage


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.requests = []
        self.uploads = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (AnswerCallbackQuery, SendChatAction)):
            return True
        if isinstance(method, (SendAudio, SendVoice)):
            file = method.audio if isinstance(method, SendAudio) else method.voice
            self.uploads.append((type(method), b"".join([chunk async for chunk in file.read(bot)])))
        return Message(
            message_id=len(self.requests) + 100,
            date=datetime.now(UTC),
            chat=Chat(id=int(method.chat_id), type="private"),
            text=method.text if isinstance(method, (SendMessage, EditMessageText)) else None,
        ).as_(bot)

    async def stream_content(self, *args, **kwargs):
        yield b""


class FakeMedia:
    """Network boundary double; real DSP is exercised in test_media.py."""

    def __init__(self, storage):
        self.storage = storage
        self.rendered = []

    async def ingest(self, track_id, user_id, chat_id, url, progress):
        await progress("Готовлю MP3…")
        directory = self.storage.directory(track_id)
        directory.mkdir()
        for name in ("source.webm", "original.mp3", "cover.jpg", "thumbnail.jpg"):
            (directory / name).write_bytes(b"original")
        track = Track(
            track_id, user_id, chat_id, "Song <3", "Artist & Co", url, 60, "source.webm", time.time()
        )
        self.storage.save(track)
        return track

    async def render(self, track, effects, preview=False):
        self.rendered.append((effects, preview))
        path = self.storage.directory(track.id) / ("preview.ogg" if preview else "effects.mp3")
        path.write_bytes(b"preview" if preview else b"effects")
        return path

    async def diagnose_youtube(self, url):
        return {"title": "Test <video>", "duration": 19}


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
