from contextlib import suppress
from pathlib import Path

from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

from .access import Access
from .artwork import MAX_ARTWORK_BYTES
from .config import Settings
from .errors import UserError
from .media import MAX_MP3_BYTES, Media
from .models import Track
from .storage import Storage
from .transfers import ArtworkBuffer, MP3Writer


class TrackCache:
    """Restore an owned track on demand; callers hold its Jobs.slot throughout."""

    def __init__(self, settings: Settings, storage: Storage, media: Media, access: Access):
        self.settings, self.storage, self.media, self.access = settings, storage, media, access

    async def ensure(self, track_id: str, user_id: int, message: Message) -> Track:
        self.access.require(user_id)
        if track := self.storage.get(track_id, user_id):
            return track
        stored = self.storage.record(track_id, user_id)
        if stored is None or stored.chat_id != message.chat.id:
            raise UserError("Трек недоступен. Пришли ссылку или MP3 заново.")
        reference = self.storage.file(track_id, user_id, "audio")
        # Old bot messages still expose their audio on a callback. Never substitute an effects version.
        if (
            reference is None
            and message.audio
            and message.audio.title == stored.title
            and message.audio.performer == stored.artist
        ):
            self.storage.remember_file(
                track_id, user_id, "audio", message.audio.file_id, message.audio.file_size
            )
            reference = self.storage.file(track_id, user_id, "audio")
        if reference is None and not stored.url:
            raise UserError(
                "Файла уже нет в кэше. Перешли MP3 из диалога боту, чтобы открыть редактор снова."
            )
        status = await message.answer("⏳ Восстанавливаю трек из Telegram…")

        async def progress(text: str) -> None:
            with suppress(TelegramBadRequest):
                await status.edit_text("⏳ " + text)

        cover_raw = None
        cover = self.storage.file(track_id, user_id, "cover")
        if cover and (cover[1] is None or cover[1] <= MAX_ARTWORK_BYTES):
            with ArtworkBuffer() as buffer:
                await message.bot.download(
                    cover[0], destination=buffer, timeout=self.settings.download_timeout_seconds
                )
                cover_raw = buffer.getvalue()
        limit = min(MAX_MP3_BYTES, self.settings.max_download_bytes, self.settings.max_upload_bytes)

        async def download(path: Path) -> None:
            self.access.require(user_id)
            if reference is None:
                raise UserError("Нет Telegram-копии трека.")
            with MP3Writer(path, limit) as stream:
                await message.bot.download(
                    reference[0], destination=stream, timeout=self.settings.download_timeout_seconds
                )
            self.access.require(user_id)

        restored = None
        try:
            if reference and (reference[1] is None or reference[1] <= limit):
                try:
                    restored = await self.media.ingest_mp3(
                        track_id,
                        user_id,
                        stored.chat_id,
                        f"{stored.filename}.mp3",
                        download,
                        progress,
                        metadata_override=stored,
                        cover_raw=cover_raw,
                    )
                except (TelegramBadRequest, UserError):
                    self.access.require(user_id)
                    if not stored.url:
                        raise UserError(
                            "Не удалось скачать MP3 из Telegram. Перешли файл боту ещё раз."
                        ) from None
            if restored is None:
                self.access.require(user_id)
                if not stored.url:
                    raise UserError("Telegram не отдаёт файл такого размера. Перешли MP3 боту ещё раз.")
                # Cloud Bot API cannot download files above 20 MB; YouTube remains a second source.
                restored = await self.media.ingest(
                    track_id,
                    user_id,
                    stored.chat_id,
                    stored.url,
                    progress,
                    metadata_override=stored,
                    cover_raw=cover_raw,
                )
            self.access.require(user_id)
            await status.edit_text("✅ Трек восстановлен. Можно продолжать редактирование.")
            return restored
        except UserError as exc:
            with suppress(TelegramBadRequest):
                await status.edit_text(str(exc))
            raise
