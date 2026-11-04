import time
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .access import Access
from .config import Settings
from .errors import UserError
from .jobs import Jobs
from .media import Media
from .storage import Storage
from .youtube import ISSUES, cookie_health

PAGE_SIZE = 10


class AdminAction(CallbackData, prefix="adm"):
    action: str
    page: int = 0


class AdminInput(StatesGroup):
    add = State()
    remove = State()


def admin_button(text: str, action: str, page: int = 0) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=AdminAction(action=action, page=page).pack())


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [admin_button("👥 Разрешённые пользователи", "users")],
            [admin_button("➕ Добавить ID", "add"), admin_button("➖ Удалить ID", "remove")],
            [admin_button("🔧 Подключение YouTube", "youtube")],
            [admin_button("🔄 Обновить", "home")],
        ]
    )


def youtube_text(settings: Settings, storage: Storage) -> str:
    health = cookie_health(settings.cookies_path)
    text = (
        "🔧 <b>Подключение YouTube</b>\n\n"
        f"Сессия: {health.summary}\n"
        f"PO Token provider: {'подключён в настройках' if settings.youtube_pot_provider_url else 'не настроен'}\n"
        f"Прокси: {'подключён' if settings.youtube_proxy.get_secret_value() else 'не настроен'}\n"
        f"Интервал между загрузками: {settings.youtube_min_interval_seconds:g} сек.\n"
    )
    last = storage.get_runtime("youtube_last_failure")
    if isinstance(last, dict):
        age = max(0, int((time.time() - last["at"]) / 60))
        issue = ISSUES.get(last["code"], ISSUES["unknown"])
        text += f"\nПоследний отказ ({age} мин. назад):\n{escape(issue)}\n"
    text += (
        "\nДля подключения аккаунта импортируй cookies.txt на машине с ботом:\n"
        "<code>uv run srtube --import-cookies /путь/cookies.txt</code>\n\n"
        "Cookies читаются заново при каждой загрузке; после импорта перезапуск не нужен. "
        "Наличие файла не гарантирует, что YouTube принимает сессию. Проверка ниже проверяет получение аудиоформата."
    )
    return text


def build_admin_router(
    settings: Settings, storage: Storage, media: Media, jobs: Jobs, access: Access
) -> Router:
    router = Router(name="admin")

    async def show_home(message: Message) -> None:
        admins = ", ".join(f"<code>{value}</code>" for value in sorted(access.admins))
        await message.answer(
            "🛠 <b>Админка SRTube</b>\n\n"
            "Режим: только приглашённые пользователи.\n"
            f"Разрешённых пользователей: <b>{storage.user_count()}</b>\n"
            f"Администраторы: {admins}\n\n"
            "Добавление и удаление ID применяются сразу, без перезапуска.",
            reply_markup=admin_keyboard(),
        )

    async def show_users(message: Message, requested_page: int) -> None:
        count = storage.user_count()
        page = max(0, min(requested_page, max(0, (count - 1) // PAGE_SIZE)))
        users = storage.list_users(page * PAGE_SIZE, PAGE_SIZE)
        text = "👥 <b>Разрешённые пользователи</b>\n\n"
        text += "\n".join(f"<code>{value}</code>" for value in users) if users else "Список пока пуст."
        text += f"\n\nВсего: {count}. Администраторы имеют доступ отдельно."
        rows = []
        navigation = []
        if page > 0:
            navigation.append(admin_button("← Назад", "users", page - 1))
        if (page + 1) * PAGE_SIZE < count:
            navigation.append(admin_button("Далее →", "users", page + 1))
        if navigation:
            rows.append(navigation)
        rows += [
            [admin_button("➕ Добавить ID", "add"), admin_button("➖ Удалить ID", "remove")],
            [admin_button("↩️ Админка", "home")],
        ]
        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    @router.message(Command("admin"))
    async def admin(message: Message, state: FSMContext) -> None:
        if not message.from_user or not access.is_admin(message.from_user.id):
            await message.answer("Эта команда доступна только администратору.")
            return
        await state.clear()
        await show_home(message)

    @router.callback_query(AdminAction.filter())
    async def admin_action(query: CallbackQuery, callback_data: AdminAction, state: FSMContext) -> None:
        if not access.is_admin(query.from_user.id):
            await query.answer("Доступно только администратору.", show_alert=True)
            return
        if not isinstance(query.message, Message):
            await query.answer()
            return
        await query.answer()
        message = query.message
        await state.clear()
        action = callback_data.action
        if action == "home":
            await show_home(message)
        elif action == "users":
            await show_users(message, callback_data.page)
        elif action in {"add", "remove"}:
            await state.set_state(AdminInput.add if action == "add" else AdminInput.remove)
            verb = "добавления" if action == "add" else "удаления"
            await message.answer(
                f"Введи Telegram ID для {verb}. Можно несколько: через пробел, запятую или новую строку.\n\n"
                "Пользователь может узнать свой ID командой /id у этого бота.\n/cancel — отменить",
                reply_markup=ForceReply(selective=True, input_field_placeholder="123456789 987654321"),
            )
        elif action == "youtube":
            await message.answer(
                youtube_text(settings, storage),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [admin_button("🔎 Проверить YouTube", "check")],
                        [admin_button("↩️ Админка", "home")],
                    ]
                ),
            )
        elif action == "check":
            status = await message.answer("⏳ Проверяю подключение к YouTube…")
            try:
                async with jobs.slot(query.from_user.id, "youtube-diagnostic"):
                    result = await media.diagnose_youtube("https://www.youtube.com/watch?v=jNQXAC9IVRw")
                text = f"✅ YouTube отдал аудиоформат.\nВидео: {escape(str(result['title']))}"
            except UserError as exc:
                text = f"❌ Проверка не завершена: {escape(str(exc))}\n\n" + youtube_text(settings, storage)
            try:
                await status.edit_text(
                    text,
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [admin_button("🔧 YouTube", "youtube"), admin_button("↩️ Админка", "home")],
                        ]
                    ),
                )
            except TelegramBadRequest:
                await message.answer(text)

    @router.message(AdminInput.add, F.text & ~F.text.startswith("/"))
    @router.message(AdminInput.remove, F.text & ~F.text.startswith("/"))
    async def change_users(message: Message, state: FSMContext) -> None:
        if not message.from_user or not access.is_admin(message.from_user.id):
            await state.clear()
            return
        try:
            user_ids = Settings.parse_ids(message.text or "")
            if not user_ids or len(user_ids) > 100:
                raise ValueError
        except ValueError:
            await message.answer(
                "Нужны положительные числовые Telegram ID, максимум 100 за раз. Например: 123456789 987654321"
            )
            return
        if user_ids.intersection(access.admins):
            await message.answer(
                "Администраторы уже имеют доступ. Их ID меняются только в ADMIN_USER_IDS, отдельно от этого списка."
            )
            return
        removing = await state.get_state() == AdminInput.remove.state
        count = (
            storage.remove_users(user_ids) if removing else storage.add_users(user_ids, message.from_user.id)
        )
        await state.clear()
        await message.answer(
            f"✅ {'Удалено' if removing else 'Добавлено'}: {count}. Изменения уже действуют.",
            reply_markup=admin_keyboard(),
        )

    return router
