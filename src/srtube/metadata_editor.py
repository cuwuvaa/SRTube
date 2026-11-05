import asyncio
import re
from contextlib import suppress
from dataclasses import replace
from html import escape
from io import BufferedWriter, BytesIO
from pathlib import Path
from uuid import uuid4

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, FSInputFile, InputMediaPhoto, Message

from .access import Access
from .artwork import ARTWORK_ACTIONS, MAX_ARTWORK_BYTES
from .config import Settings
from .errors import UserError
from .jobs import Jobs
from .keyboards import MetadataAction, cover_keyboard, metadata_keyboard
from .media import MAX_MP3_BYTES, Media
from .models import YOUTUBE_MESSAGE, Track, editable_text
from .security import upload_snapshot
from .storage import Storage


class MetadataInput(StatesGroup):
    text = State()
    cover = State()


class ArtworkBuffer(BytesIO):
    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > MAX_ARTWORK_BYTES:
            raise UserError("Обложка должна быть не больше 10 МБ.")
        return super().write(data)


class MP3Writer(BufferedWriter):
    def __init__(self, path: Path, limit: int):
        super().__init__(path.open("xb", buffering=0))
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise UserError(f"MP3 должен быть не больше {self.limit // 1_000_000} МБ.")
        return super().write(data)


def is_mp3_upload(message: Message) -> bool:
    file = message.document
    return message.audio is not None or bool(
        file
        and ((file.file_name or "").lower().endswith(".mp3") or file.mime_type in {"audio/mpeg", "audio/mp3"})
    )


def metadata_text(track: Track) -> str:
    return (
        f"✏️ <b>Метаданные трека</b>\n\n"
        f"🎤 Исполнитель: <b>{escape(track.artist)}</b>\n"
        f"🎵 Название: <b>{escape(track.title)}</b>\n"
        f"💿 Альбом: <b>{escape(track.album) if track.album else 'не указан'}</b>\n\n"
        "Изменения сохраняются сразу и применяются к исходному MP3 и новым версиям slowed + reverb.\n"
        "Нажми «Получить MP3», чтобы скачать обновлённый файл."
    )


def cover_text(track: Track) -> str:
    return (
        f"🎨 <b>Обложка</b>\n{escape(track.artist)} — {escape(track.title)}\n\n"
        "Кадрируй в квадрат или оставь поля, настрой масштаб, положение, поворот и яркость. "
        "Стрелки работают при кадрировании или масштабе выше 100%.\n"
        "Правки сохраняются сразу. Сброс вернёт последнюю загруженную обложку без правок."
    )


def is_editor_input(message: Message) -> bool:
    text = message.text or ""
    return not text.startswith("/") and not re.match(YOUTUBE_MESSAGE, text)


def build_metadata_router(
    settings: Settings, storage: Storage, media: Media, jobs: Jobs, access: Access
) -> Router:
    router = Router(name="metadata")

    def require_track(track_id: str, user_id: int) -> Track:
        access.require(user_id)
        track = storage.get(track_id, user_id)
        if track is None:
            raise UserError("Трек больше не в кэше. Пришли ссылку или MP3 заново.")
        return track

    async def persist(track: Track, **kwargs) -> None:
        async def save() -> None:
            await media.edit_metadata(track, **kwargs)
            storage.save(track)

        task = asyncio.create_task(save())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def show_cover(message: Message, track: Track, *, edit: bool = False) -> None:
        access.require(track.user_id)
        async with upload_snapshot(
            storage.root, [(storage.directory(track.id) / "cover.jpg", 10_000_000)]
        ) as files:
            photo = FSInputFile(files[0])
            if edit and message.photo:
                try:
                    await message.edit_media(
                        media=InputMediaPhoto(media=photo, caption=cover_text(track)),
                        reply_markup=cover_keyboard(track.id),
                    )
                except TelegramBadRequest as exc:
                    if "message is not modified" not in exc.message.lower():
                        raise
            else:
                await message.answer_photo(
                    photo=photo, caption=cover_text(track), reply_markup=cover_keyboard(track.id)
                )

    @router.callback_query(MetadataAction.filter())
    async def on_metadata(query: CallbackQuery, callback_data: MetadataAction, state: FSMContext) -> None:
        message = query.message
        if not isinstance(message, Message):
            await query.answer("Открой диалог с ботом.", show_alert=True)
            return
        try:
            track = require_track(callback_data.track, query.from_user.id)
        except UserError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        action = callback_data.action
        await query.answer()
        if action == "open":
            await state.clear()
            await message.answer(metadata_text(track), reply_markup=metadata_keyboard(track.id))
        elif action in {"title", "artist", "album", "upload"}:
            await state.clear()
            await state.set_state(MetadataInput.cover if action == "upload" else MetadataInput.text)
            await state.update_data(track_id=track.id, field=action)
            if action == "upload":
                await message.answer(
                    "Пришли новую обложку как фото или файл JPEG, PNG, WebP.\n"
                    "До 10 МБ и 8 мегапикселей. Файл сохраняет качество лучше, чем фото.\n/cancel — отменить ввод"
                )
            else:
                label = {"title": "название трека", "artist": "имя исполнителя", "album": "название альбома"}[
                    action
                ]
                await message.answer(
                    f"Введи {label}: от 1 до 180 символов.\n/cancel — отменить ввод",
                    reply_markup=ForceReply(selective=True),
                )
        elif action == "clear_album":
            try:
                async with jobs.slot(query.from_user.id, track.id):
                    track = replace(require_track(track.id, query.from_user.id), album="")
                    await persist(track)
                    await state.clear()
                    access.require(query.from_user.id)
                    await message.answer(
                        "✅ Альбом удалён.\n\n" + metadata_text(track),
                        reply_markup=metadata_keyboard(track.id),
                    )
            except UserError as exc:
                await message.answer(str(exc))
        elif action == "cover" or action in ARTWORK_ACTIONS:
            await state.clear()
            try:
                async with jobs.slot(query.from_user.id, track.id):
                    track = require_track(track.id, query.from_user.id)
                    if action != "cover":
                        await persist(track, cover_action=action)
                    await show_cover(message, track, edit=action != "cover")
            except UserError as exc:
                await message.answer(str(exc))

    # An incoming track starts its own editor even while another text/cover input is pending.
    @router.message(is_mp3_upload)
    async def receive_mp3(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        file = message.audio or message.document
        if file is None:
            return
        status = None
        limit = min(MAX_MP3_BYTES, settings.max_download_bytes, settings.max_upload_bytes)
        try:
            if file.file_size is not None and not 0 < file.file_size <= limit:
                raise UserError(f"MP3 должен быть не больше {limit // 1_000_000} МБ.")
            track_id = uuid4().hex[:12]
            async with jobs.slot(message.from_user.id, track_id):
                access.require(message.from_user.id)
                await state.clear()
                status = await message.answer("⏳ Принимаю MP3 для редактирования метаданных…")

                async def progress(text: str) -> None:
                    with suppress(TelegramBadRequest):
                        await status.edit_text("⏳ " + text)

                async def download(path: Path) -> None:
                    try:
                        with MP3Writer(path, limit) as stream:
                            await message.bot.download(
                                file.file_id, destination=stream, timeout=settings.download_timeout_seconds
                            )
                    except TelegramBadRequest as exc:
                        if "file is too big" in exc.message.lower():
                            raise UserError(f"MP3 должен быть не больше {limit // 1_000_000} МБ.") from None
                        raise

                track = await media.ingest_mp3(
                    track_id,
                    message.from_user.id,
                    message.chat.id,
                    file.file_name or "Трек.mp3",
                    download,
                    progress,
                )
                access.require(message.from_user.id)
                await status.edit_text(
                    "✅ MP3 принят. Выбери, что изменить.\n\n" + metadata_text(track),
                    reply_markup=metadata_keyboard(track.id),
                )
        except UserError as exc:
            if status is not None:
                await status.edit_text(str(exc))
            else:
                await message.answer(str(exc))

    @router.message(MetadataInput.text, is_editor_input)
    @router.message(MetadataInput.cover, is_editor_input)
    async def receive_metadata(message: Message, state: FSMContext) -> None:
        if message.from_user is None:
            return
        data = await state.get_data()
        field = data.get("field")
        try:
            value = None
            file = None
            if field in {"title", "artist", "album"}:
                if message.text is None:
                    raise UserError("Нужен текст от 1 до 180 символов. /cancel — отменить ввод.")
                value = editable_text(message.text)
            elif field == "upload":
                file = message.photo[-1] if message.photo else message.document
                if file is None:
                    raise UserError(
                        "Пришли обложку как фото или файл JPEG, PNG, WebP. /cancel — отменить ввод."
                    )
                if file.file_size is not None and file.file_size > MAX_ARTWORK_BYTES:
                    raise UserError("Обложка должна быть не больше 10 МБ.")
            else:
                await state.clear()
                return
            track_id = data.get("track_id", "")
            async with jobs.slot(message.from_user.id, track_id):
                track = require_track(track_id, message.from_user.id)
                raw = None
                if file is not None:
                    with ArtworkBuffer() as buffer:
                        await message.bot.download(file.file_id, destination=buffer)
                        raw = buffer.getvalue()
                if value is not None:
                    track = replace(track, **{field: value})
                access.require(message.from_user.id)
                await persist(track, raw=raw)
                await state.clear()
                access.require(message.from_user.id)
                if file is not None:
                    await show_cover(message, track)
                else:
                    await message.answer(
                        "✅ Сохранено.\n\n" + metadata_text(track), reply_markup=metadata_keyboard(track.id)
                    )
        except UserError as exc:
            if storage.get(data.get("track_id", ""), message.from_user.id) is None:
                await state.clear()
            await message.answer(str(exc))

    return router
