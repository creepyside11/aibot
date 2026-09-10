"""Выбор веб-учителя и безопасный ввод cookies из Telegram-админки."""
from __future__ import annotations

import contextlib
import html

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config

router = Router(name="admin_provider")
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)


class ProviderSetup(StatesGroup):
    cookie = State()


def _kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="♊ Gemini", callback_data="a:provider:gemini"),
            InlineKeyboardButton(text="🤖 ChatGPT", callback_data="a:provider:chatgpt"),
        ],
        [InlineKeyboardButton(text="⬅️ Настройки", callback_data="a:cfg")],
    ])


@router.callback_query(F.data.startswith("a:provider:"))
async def choose_provider(call: CallbackQuery, state: FSMContext, teacher, **_):
    provider = call.data.rsplit(":", 1)[1]
    if provider not in ("gemini", "chatgpt"):
        await call.answer("Неизвестный источник", show_alert=True)
        return
    await state.set_state(ProviderSetup.cookie)
    await state.update_data(provider=provider)
    title = "Gemini" if provider == "gemini" else "ChatGPT"
    saved = "есть" if teacher.has_cookies(provider) else "нет"
    text = (
        "<b>🔐 Cookies для %s</b>\n\n"
        "Сохранённые cookies: <b>%s</b>.\n"
        "Пришли новые cookies <b>одним текстовым сообщением</b>. Подойдут:\n"
        "• JSON из Cookie-Editor / EditThisCookie;\n"
        "• cookies.txt (Netscape);\n"
        "• <code>Cookie: name=value; name2=value2</code>.\n\n"
        "Сообщение бот сразу попытается удалить. Cookies хранятся только в <code>data/</code>."
    ) % (title, saved)
    try:
        await call.message.edit_text(text, reply_markup=_kb())
    finally:
        with contextlib.suppress(Exception):
            await call.answer()


@router.message(StateFilter(ProviderSetup.cookie))
async def receive_cookies(message: Message, state: FSMContext, teacher, **_):
    raw = (message.text or "").strip()
    data = await state.get_data()
    provider = data.get("provider", "")

    with contextlib.suppress(Exception):
        await message.delete()

    if not raw or provider not in ("gemini", "chatgpt"):
        await state.clear()
        await message.bot.send_message(
            message.chat.id,
            "❌ Не получил cookies. Открой /admin → Настройки и попробуй снова.",
            reply_markup=_kb(),
        )
        return

    try:
        ok, detail = await teacher.switch(provider, raw)
    except Exception as exc:
        ok, detail = False, str(exc)
    await state.clear()

    title = "Gemini" if provider == "gemini" else "ChatGPT"
    if ok:
        text = (
            "✅ <b>%s подключён</b>\n%s\n\n"
            "Новые неизвестные вопросы теперь идут к этому источнику."
        ) % (title, html.escape(detail or "cookies приняты"))
    else:
        text = (
            "❌ <b>Не удалось подключить %s</b>\n<code>%s</code>\n\n"
            "Проверь, что cookies свежие и взяты из уже авторизованной вкладки."
        ) % (title, html.escape((detail or "неизвестная ошибка")[:700]))
    await message.bot.send_message(message.chat.id, text, reply_markup=_kb())
