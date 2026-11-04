from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .models import Effects


class Action(CallbackData, prefix="fx"):
    action: str
    track: str
    slowed: int = 15
    reverb: int = 25


def button(text: str, action: str, track: str, effects: Effects) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=text,
        callback_data=Action(action=action, track=track, slowed=effects.slowed, reverb=effects.reverb).pack(),
    )


def track_keyboard(track: str, effects: Effects | None = None) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("🎛 Сделать slowed + reverb", "open", track, effects or Effects()),
            ]
        ]
    )


def effects_keyboard(track: str, effects: Effects) -> InlineKeyboardMarkup:
    def change(slowed: int = 0, reverb: int = 0) -> Effects:
        return Effects(max(0, min(50, effects.slowed + slowed)), max(0, min(100, effects.reverb + reverb)))

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("−5%", "set", track, change(slowed=-5)),
                button(f"Slowed: {effects.slowed}%", "hint", track, effects),
                button("+5%", "set", track, change(slowed=5)),
            ],
            [
                button("−5%", "set", track, change(reverb=-5)),
                button(f"Reverb: {effects.reverb}%", "hint", track, effects),
                button("+5%", "set", track, change(reverb=5)),
            ],
            [button("✍️ Ввести точные проценты", "custom", track, effects)],
            [button("▶️ Голосовое превью", "preview", track, effects)],
            [button("⬇️ Получить полный MP3", "render", track, effects)],
            [button("🎵 Исходный MP3", "original", track, effects)],
        ]
    )


def preview_keyboard(track: str, effects: Effects) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("⬇️ Получить полный MP3", "render", track, effects)],
            [button("🎛 Изменить параметры", "open", track, effects)],
        ]
    )
