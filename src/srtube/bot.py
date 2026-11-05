import asyncio
import logging
from contextlib import suppress
from html import escape
from typing import Any
from uuid import uuid4

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    ForceReply,
    FSInputFile,
    Message,
    TelegramObject,
)
from aiogram.utils.chat_action import ChatActionSender

from .access import Access, AccessMiddleware
from .admin import build_admin_router
from .config import Settings
from .errors import UserError
from .jobs import Jobs
from .keyboards import Action, effects_keyboard, preview_keyboard, track_keyboard
from .media import Media
from .metadata_editor import build_metadata_router
from .models import YOUTUBE_MESSAGE, Effects, Track, parse_effects, youtube_url
from .security import upload_snapshot
from .storage import Storage

logger = logging.getLogger(__name__)


class CustomEffects(StatesGroup):
    values = State()


class UpdateTasks(BaseMiddleware):
    """Stop media subprocesses before closing the bot session and database."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task] = set()
        self.stopping = False

    async def __call__(self, handler: Any, event: TelegramObject, data: dict) -> Any:
        if self.stopping:
            return None
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Update has no asyncio task")
        self.tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self.tasks.discard(task)

    async def shutdown(self, **kwargs: Any) -> None:
        self.stopping = True
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def panel_text(track: Track, effects: Effects, preview_seconds: int) -> str:
    return (
        f"🎛 <b>Slowed + reverb</b>\n\n"
        f"<b>{escape(track.title)}</b>\n{escape(track.artist)}\n\n"
        f"Замедление: <b>{effects.slowed}%</b> · скорость {100 - effects.slowed}%\n"
        f"Реверберация: <b>{effects.reverb}%</b>\n\n"
        f"Превью — до {preview_seconds} секунд из середины трека.\n"
        "Настрой звук, послушай превью и скачай полный MP3."
    )


async def safe_edit(message: Message, text: str, **kwargs: Any) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


async def send_track(
    message: Message, track: Track, storage: Storage, effects: Effects | None = None
) -> None:
    directory = storage.directory(track.id)
    path = directory / ("effects.mp3" if effects else "original.mp3")
    title = track.title if effects is None else f"{track.title} ({effects.suffix})"
    caption = (
        "🎵 MP3 · исходное качество · метаданные"
        if not track.url and effects is None
        else "🎵 MP3 · 320 кбит/с · теги и обложка"
    )
    if track.album:
        caption += f"\n💿 {escape(track.album)}"
    if effects:
        caption += f"\n🎛 Slowed {effects.slowed}% · Reverb {effects.reverb}%"
    async with upload_snapshot(
        storage.root, [(path, 49_000_000), (directory / "thumbnail.jpg", 200_000)]
    ) as files:
        await message.answer_audio(
            audio=FSInputFile(
                files[0], filename=f"{track.filename}{' (' + effects.suffix + ')' if effects else ''}.mp3"
            ),
            thumbnail=FSInputFile(files[1]),
            title=title,
            performer=track.artist,
            caption=caption,
            reply_markup=track_keyboard(track.id, effects),
            request_timeout=180,
        )


def build_dispatcher(settings: Settings, storage: Storage, media: Media, jobs: Jobs) -> Dispatcher:
    dp = Dispatcher()
    updates = UpdateTasks()
    dp.update.outer_middleware(updates)
    dp.shutdown.register(updates.shutdown)
    access = Access(settings, storage)
    dp.message.filter(F.chat.type == "private")
    dp.callback_query.filter(F.message.chat.type == "private")
    dp.message.outer_middleware(AccessMiddleware(access))
    dp.callback_query.outer_middleware(AccessMiddleware(access))
    dp.include_router(build_admin_router(settings, storage, media, jobs, access))
    dp.include_router(build_metadata_router(settings, storage, media, jobs, access))
    router = Router(name="srtube")

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def start(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "🎧 <b>SRTube</b>\n\n"
            "Пришли ссылку на YouTube — получишь MP3 320 кбит/с с названием, исполнителем и обложкой.\n\n"
            "Или пришли свой <b>MP3</b> как аудио или документ — откроется редактор его метаданных.\n\n"
            "Кнопка <b>«Редактировать метаданные»</b> меняет название, исполнителя, альбом и обложку: "
            "загрузи свою или отредактируй существующую.\n\n"
            "Под треком нажми <b>«Сделать slowed + reverb»</b>: выбери проценты, "
            "послушай голосовое превью из середины и скачай полную версию.\n\n"
            "Slowed: 0–50% (снижает скорость и тон). Reverb: 0–100% (доля реверберации).\n"
            f"Видео и MP3 до {settings.max_duration_seconds // 60} мин.; готовый файл — до 50 МБ.\n"
            "Входящий MP3 — до 20 МБ.\n\n"
            "/last — последний трек\n/id — мой Telegram ID\n/cancel — отменить ввод",
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Ввод отменён. Можно использовать кнопки под треком или прислать ссылку либо MP3."
        )

    @router.message(Command("last"))
    async def last(message: Message, state: FSMContext) -> None:
        await state.clear()
        if message.from_user is None:
            return
        track = storage.latest(message.from_user.id)
        if track is None:
            await message.answer("В кэше пока нет твоих треков. Пришли ссылку на YouTube или свой MP3.")
            return
        try:
            async with jobs.slot(message.from_user.id, track.id):
                access.require(message.from_user.id)
                async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                    await send_track(message, track, storage)
        except UserError as exc:
            await message.answer(str(exc))

    # URLs take precedence over a pending custom input, so a user can switch tracks immediately.
    @router.message(F.text.regexp(YOUTUBE_MESSAGE))
    async def receive_url(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        status = None
        try:
            url = youtube_url(message.text or "")
            await state.clear()
            cached = storage.cached_url(url, message.from_user.id)
            track_id = cached.id if cached else uuid4().hex[:12]
            status = await message.answer("⏳ Принял ссылку. Ожидаю свободное место в обработке…")
            async with jobs.slot(message.from_user.id, track_id):
                access.require(message.from_user.id)

                async def progress(text: str) -> None:
                    with suppress(TelegramBadRequest):
                        await safe_edit(status, "⏳ " + text)

                async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                    track = storage.get(track_id, message.from_user.id) if cached else None
                    if track is None:
                        track = await media.ingest(
                            track_id,
                            message.from_user.id,
                            message.chat.id,
                            url,
                            progress,
                        )
                    await progress("Отправляю MP3…")
                    access.require(message.from_user.id)
                    await send_track(message, track, storage)
                await safe_edit(status, "✅ Готово. Настрой slowed + reverb кнопкой под треком.")
        except UserError as exc:
            if status:
                await safe_edit(status, str(exc))
            else:
                await message.answer(str(exc))

    @router.callback_query(Action.filter())
    async def on_action(query: CallbackQuery, callback_data: Action, state: FSMContext) -> None:
        message = query.message
        if not isinstance(message, Message):
            await query.answer("Открой диалог с ботом.", show_alert=True)
            return
        track = storage.get(callback_data.track, query.from_user.id)
        if track is None:
            await query.answer("Трек больше не в кэше. Пришли ссылку или MP3 заново.", show_alert=True)
            return
        try:
            effects = Effects(callback_data.slowed, callback_data.reverb)
        except UserError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        action = callback_data.action
        if action == "hint":
            await query.answer("Используй ±5% или «Ввести точные проценты».")
            return
        await query.answer()
        if action in {"open", "set"}:
            await state.clear()
            kwargs = {"reply_markup": effects_keyboard(track.id, effects)}
            text = panel_text(track, effects, settings.preview_seconds)
            if action == "set" and message.text:
                await safe_edit(message, text, **kwargs)
            else:
                await message.answer(text, **kwargs)
            return
        if action == "custom":
            prompt = await message.answer(
                "Введи два целых процента: <b>slowed reverb</b>.\n"
                "Например: <code>17 35</code>\n\n"
                "Slowed: 0–50%. Reverb: 0–100%.\n/cancel — отменить ввод",
                reply_markup=ForceReply(selective=True, input_field_placeholder="17 35"),
            )
            await state.set_state(CustomEffects.values)
            await state.update_data(track_id=track.id, prompt_id=prompt.message_id)
            return
        if action not in {"preview", "render", "original"}:
            return
        status = None
        path = None
        try:
            status = await message.answer("⏳ Принял запрос. Ожидаю свободное место в обработке…")
            async with jobs.slot(query.from_user.id, track.id):
                access.require(query.from_user.id)
                # Re-check after waiting in the global queue; protected tracks cannot be pruned.
                track = storage.get(track.id, query.from_user.id)
                if track is None:
                    raise UserError("Трек больше не в кэше. Пришли ссылку или MP3 заново.")
                await safe_edit(
                    status,
                    "⏳ Готовлю голосовое превью…" if action == "preview" else "⏳ Готовлю MP3…",
                )
                async with ChatActionSender.upload_voice(bot=message.bot, chat_id=message.chat.id):
                    if action == "preview":
                        path = await media.render(track, effects, preview=True)
                        access.require(query.from_user.id)
                        async with upload_snapshot(storage.root, [(path, 2_000_000)]) as files:
                            await message.answer_voice(
                                voice=FSInputFile(files[0]),
                                caption=f"🎧 Превью из середины · Slowed {effects.slowed}% · Reverb {effects.reverb}%",
                                reply_markup=preview_keyboard(track.id, effects),
                                request_timeout=120,
                            )
                    elif action == "render":
                        path = await media.render(track, effects)
                        access.require(query.from_user.id)
                        await send_track(message, track, storage, effects)
                    else:
                        await send_track(message, track, storage)
                await safe_edit(status, "✅ Превью готово." if action == "preview" else "✅ MP3 отправлен.")
        except UserError as exc:
            if status:
                await safe_edit(status, str(exc))
            else:
                await message.answer(str(exc))
        finally:
            if path:
                path.unlink(missing_ok=True)

    @router.message(CustomEffects.values, F.text)
    async def custom_values(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        try:
            effects = parse_effects(message.text or "")
        except UserError as exc:
            await message.answer(str(exc))
            return
        data = await state.get_data()
        track = storage.get(data.get("track_id", ""), message.from_user.id)
        await state.clear()
        if track is None:
            await message.answer("Трек больше не в кэше. Пришли ссылку или MP3 заново.")
            return
        await message.answer(
            panel_text(track, effects, settings.preview_seconds),
            reply_markup=effects_keyboard(track.id, effects),
        )

    @router.message()
    async def fallback(message: Message) -> None:
        await message.answer(
            "Пришли ссылку на видео YouTube или MP3 как аудио или документ. /help — как пользоваться ботом."
        )

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        # Avoid printing URLs, cookies, bot tokens, or upstream request data in logs.
        logger.error("Update failed: %s", type(event.exception).__name__)
        message = event.update.message
        if event.update.callback_query and isinstance(event.update.callback_query.message, Message):
            message = event.update.callback_query.message
        if message:
            with suppress(Exception):
                await message.answer(
                    "Не получилось завершить обработку. Попробуй ещё раз. /last — последний готовый трек."
                )
        return True

    dp.include_router(router)
    return dp


async def cleanup_loop(settings: Settings, storage: Storage, jobs: Jobs) -> None:
    while True:
        try:
            storage.cleanup(settings.cache_hours, settings.max_cache_bytes, jobs.protected)
        except OSError as exc:
            logger.warning("Cache cleanup failed: %s", type(exc).__name__)
        await asyncio.sleep(300)
