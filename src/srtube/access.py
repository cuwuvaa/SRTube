from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .config import Settings
from .errors import UserError
from .security import RateLimit
from .storage import Storage


class Access:
    def __init__(self, settings: Settings, storage: Storage):
        self.admins = settings.admin_ids
        self.storage = storage
        storage.seed_access(settings.allowed_ids)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admins

    def permitted(self, user_id: int) -> bool:
        return self.is_admin(user_id) or self.storage.is_allowed(user_id)

    def require(self, user_id: int) -> None:
        if not self.permitted(user_id):
            raise UserError("🔒 Доступ к боту закрыт. Обратись к владельцу, чтобы он добавил твой ID.")


class AccessMiddleware(BaseMiddleware):
    def __init__(self, access: Access):
        self.access = access
        self.rate = RateLimit(burst=12)
        self.known_rate = RateLimit(burst=40, capacity=1)
        self.unknown_rate = RateLimit(burst=10, period=60, capacity=1)

    async def __call__(self, handler: Any, event: TelegramObject, data: dict) -> Any:
        user = getattr(event, "from_user", None)
        permitted = bool(user and self.access.permitted(user.id))
        global_rate = self.known_rate if permitted else self.unknown_rate
        if not global_rate.allow(0):
            return None
        if user and not self.rate.allow(user.id):
            return None
        if isinstance(event, Message) and user:
            command = (event.text or "").strip().split(maxsplit=1)[0:1]
            if command and command[0].split("@")[0].lower() == "/id":
                await event.answer(f"Твой Telegram ID: <code>{user.id}</code>")
                return None
        if user is None or not permitted:
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ к боту закрыт. Обратись к владельцу.", show_alert=True)
            elif isinstance(event, Message) and user:
                await event.answer(
                    "🔒 Бот доступен только приглашённым пользователям.\n"
                    f"Твой ID: <code>{user.id}</code>\nПередай его владельцу бота для добавления в список."
                )
            return None
        return await handler(event, data)
