"""Админ-панель: подробная статистика и управление ИИ. Только для ADMIN_ID."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import config
from tgbot.format import bar, human_time, user_title

log = logging.getLogger(__name__)
router = Router(name="admin")
router.message.filter(F.from_user.id == config.ADMIN_ID)
router.callback_query.filter(F.from_user.id == config.ADMIN_ID)


class Admin(StatesGroup):
    broadcast = State()
    confirm = State()
    kb_search = State()
    kb_forget = State()


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=data) for title, data in row]
        for row in rows
    ])


MAIN_KB = kb([
    [("📊 Обзор", "a:home"), ("📈 Активность", "a:act")],
    [("👥 Пользователи", "a:users"), ("🧠 База знаний", "a:kb")],
    [("🩺 Состояние", "a:health"), ("⚙️ Настройки", "a:cfg")],
    [("📢 Рассылка", "a:cast"), ("♻️ Обновить", "a:home")],
])
BACK_KB = kb([[("⬅️ Назад", "a:home")]])


def _pct(part, whole) -> str:
    return "%.1f%%" % (100.0 * part / whole) if whole else "0.0%"


# ---------------------------------------------------------------- обзор
MEDIA_NAMES = {"photo": "фото", "voice": "голосовые", "audio": "аудио",
               "video": "видео", "video_note": "кружки", "document": "файлы",
               "sticker": "стикеры", "mixed": "альбомы"}


def overview_text(storage, teacher, autolearner) -> str:
    d = storage.overview()
    uptime = human_time(time.time() - storage.started_at)
    kinds = storage.media_breakdown()
    media_kinds = "\n  " + ", ".join(
        "%s %d" % (MEDIA_NAMES.get(r["kind"], r["kind"]), r["c"]) for r in kinds
    ) if kinds else ""
    return (
        "<b>📊 Панель управления {name}</b>\n"
        "<i>обновлено {clock} · аптайм {uptime}</i>\n\n"
        "<b>👥 Пользователи</b>\n"
        "• всего: <b>{users_total}</b>   заблокировано: {users_blocked}\n"
        "• новых сегодня: <b>{users_today}</b>   за 7 дней: {users_week}\n"
        "• активных за сутки: <b>{users_active_day}</b>\n\n"
        "<b>💬 Сообщения</b>\n"
        "• всего: <b>{msg_total}</b>   сегодня: <b>{msg_today}</b>   за неделю: {msg_week}\n"
        "• из своей памяти: <b>{from_brain}</b> ({brain_share:.1f}%)\n"
        "• спрошено у учителя: <b>{from_teacher}</b>\n"
        "• сегодня: память {brain_today} / учитель {teacher_today}\n"
        "• с вложениями: <b>{media_total}</b> (сегодня {media_today}){media_kinds}\n\n"
        "<b>🧠 База знаний</b>\n"
        "• выучено пар: <b>{kb_total}</b>   объём: {kb_size:.1f} КБ\n"
        "• новых сегодня: <b>{kb_today}</b>   за неделю: {kb_week}\n"
        "• пригодилось раз: <b>{kb_hits}</b>   работающих записей: {kb_used}\n\n"
        "<b>⚡️ Скорость</b>\n"
        "• из памяти: <b>{lat_brain:.0f} мс</b>\n"
        "• через учителя: <b>{lat_teacher:.1f} с</b>\n\n"
        "<b>🔌 Источник знаний:</b> {teacher}\n"
        "<b>🎓 Самообучение:</b> {autolearn}\n"
        "<b>⚠️ Ошибок за сутки:</b> {errors_day} (всего {errors_total})"
    ).format(
        name=html.escape(config.BOT_NAME), clock=time.strftime("%H:%M:%S"), uptime=uptime,
        users_total=d["users_total"], users_blocked=d["users_blocked"],
        users_today=d["users_today"], users_week=d["users_week"],
        users_active_day=d["users_active_day"],
        msg_total=d["msg_total"], msg_today=d["msg_today"], msg_week=d["msg_week"],
        from_brain=d["from_brain"], brain_share=d["brain_share"],
        from_teacher=d["from_teacher"], brain_today=d["brain_today"],
        teacher_today=d["teacher_today"],
        media_total=d["media_total"], media_today=d["media_today"],
        media_kinds=media_kinds,
        kb_total=d["kb_total"], kb_size=d["kb_size_kb"], kb_today=d["kb_today"],
        kb_week=d["kb_week"], kb_hits=d["kb_hits"], kb_used=d["kb_used"],
        lat_brain=d["lat_brain"] or 0, lat_teacher=(d["lat_teacher"] or 0) / 1000.0,
        teacher=html.escape(teacher.title),
        autolearn=("включено" if storage.flag("autolearn", False) else "выключено")
        + (" · выучено %d" % autolearner.learned if autolearner else ""),
        errors_day=d["errors_day"], errors_total=d["errors_total"],
    )


@router.message(Command("admin"))
async def open_panel(message: Message, storage, teacher, autolearner, state: FSMContext, **_):
    await state.clear()
    storage.upsert_user(message.from_user)
    await message.answer(overview_text(storage, teacher, autolearner), reply_markup=MAIN_KB)


async def show(call: CallbackQuery, text: str, markup=None) -> None:
    try:
        await call.message.edit_text(text, reply_markup=markup or MAIN_KB,
                                     disable_web_page_preview=True)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise
    with contextlib.suppress(Exception):
        await call.answer()


@router.callback_query(F.data == "a:home")
async def cb_home(call: CallbackQuery, storage, teacher, autolearner, **_):
    await show(call, overview_text(storage, teacher, autolearner))


# ---------------------------------------------------------------- активность
@router.callback_query(F.data == "a:act")
async def cb_activity(call: CallbackQuery, storage, **_):
    rows = storage.activity(7)
    if not rows:
        await show(call, "<b>📈 Активность</b>\n\nПока нет ни одного сообщения.", BACK_KB)
        return
    top = max(r["msgs"] for r in rows) or 1
    lines = ["<b>📈 Активность за 7 дней</b>", ""]
    for r in rows:
        lines.append(
            "<code>%s</code> %-12s <b>%d</b> сообщ. · %d чел. · из памяти %d"
            % (r["label"], bar(r["msgs"], top), r["msgs"], r["users"], r["brain"] or 0)
        )
    total = sum(r["msgs"] for r in rows)
    brain = sum(r["brain"] or 0 for r in rows)
    lines += ["", "Итого: <b>%d</b> сообщений, из них своим умом <b>%s</b>"
              % (total, _pct(brain, total))]
    await show(call, "\n".join(lines), BACK_KB)


# ---------------------------------------------------------------- пользователи
@router.callback_query(F.data == "a:users")
async def cb_users(call: CallbackQuery, storage, **_):
    top = storage.top_users(10)
    fresh = storage.recent_users(5)
    lines = ["<b>👥 Пользователи</b>", "", "<b>Самые активные</b>"]
    if not top:
        lines.append("— пока никого")
    for i, row in enumerate(top, 1):
        seen = time.strftime("%d.%m %H:%M", time.localtime(row["last_seen"] or 0))
        mark = " 🚫" if row["blocked"] else ""
        lines.append("%d. %s — <b>%d</b> сообщ. · был %s%s"
                     % (i, user_title(row), row["messages"], seen, mark))
    lines += ["", "<b>Последние пришедшие</b>"]
    for row in fresh:
        came = time.strftime("%d.%m %H:%M", time.localtime(row["created_at"] or 0))
        lines.append("• %s — %s" % (user_title(row), came))
    lines += ["", "<i>Заблокировать: /block ID · разблокировать: /unblock ID</i>"]
    await show(call, "\n".join(lines), BACK_KB)


@router.message(Command("block"))
async def cmd_block(message: Message, storage, **_):
    await _switch_block(message, storage, True)


@router.message(Command("unblock"))
async def cmd_unblock(message: Message, storage, **_):
    await _switch_block(message, storage, False)


async def _switch_block(message: Message, storage, blocked: bool) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Формат: <code>%s 123456789</code>" % parts[0])
        return
    user_id = int(parts[1])
    if storage.user_card(user_id) is None:
        await message.answer("Такого пользователя в базе нет.")
        return
    storage.set_blocked(user_id, blocked)
    await message.answer("Пользователь <code>%d</code> %s." %
                         (user_id, "заблокирован 🚫" if blocked else "разблокирован ✅"))


# ---------------------------------------------------------------- база знаний
def kb_view(storage) -> str:
    d = storage.overview()
    top = storage.knowledge_top(7)
    fresh = storage.knowledge_recent(5)
    lines = [
        "<b>🧠 База знаний</b>", "",
        "Выучено пар: <b>%d</b> · объём <b>%.1f КБ</b>" % (d["kb_total"], d["kb_size_kb"]),
        "Новых сегодня: <b>%d</b> · за неделю: %d" % (d["kb_today"], d["kb_week"]),
        "Ответов из памяти: <b>%d</b> (%s всех ответов)"
        % (d["from_brain"], _pct(d["from_brain"], (d["from_brain"] or 0) + (d["from_teacher"] or 0))),
        "Порог узнавания: <b>%.2f</b>" % storage.threshold(),
    ]
    if top:
        lines += ["", "<b>Чаще всего пригождается</b>"]
        for row in top:
            if not row["hits"]:
                continue
            lines.append("• <code>#%d</code> %s — <b>%d</b> раз"
                         % (row["id"], html.escape((row["question"] or "")[:60]), row["hits"]))
    if fresh:
        lines += ["", "<b>Выучено последним</b>"]
        for row in fresh:
            when = time.strftime("%d.%m %H:%M", time.localtime(row["created_at"] or 0))
            lines.append("• <code>#%d</code> %s <i>(%s)</i>"
                         % (row["id"], html.escape((row["question"] or "")[:60]), when))
    return "\n".join(lines)


KB_KB = kb([
    [("🔍 Найти", "a:kb:find"), ("🗑 Забыть", "a:kb:forget")],
    [("💣 Стереть всю память", "a:kb:wipe")],
    [("⬅️ Назад", "a:home")],
])


@router.callback_query(F.data == "a:kb")
async def cb_kb(call: CallbackQuery, storage, state: FSMContext, **_):
    await state.clear()
    await show(call, kb_view(storage), KB_KB)


@router.callback_query(F.data == "a:kb:find")
async def cb_kb_find(call: CallbackQuery, state: FSMContext, **_):
    await state.set_state(Admin.kb_search)
    await show(call, "🔍 Пришли текст — покажу, что ИИ знает по этой теме.",
               kb([[("⬅️ Отмена", "a:kb")]]))


@router.message(StateFilter(Admin.kb_search))
async def kb_search_input(message: Message, storage, state: FSMContext, **_):
    await state.clear()
    rows = storage.knowledge_search(message.text or "", 8)
    if not rows:
        await message.answer("Ничего не нашёл в базе знаний.", reply_markup=KB_KB)
        return
    lines = ["<b>🔍 Найдено</b>", ""]
    for row in rows:
        lines.append("<code>#%d</code> <b>%s</b>\n%s\n"
                     % (row["id"], html.escape((row["question"] or "")[:80]),
                        html.escape((row["answer"] or "")[:180])))
    await message.answer("\n".join(lines)[:4000], reply_markup=KB_KB)


@router.callback_query(F.data == "a:kb:forget")
async def cb_kb_forget(call: CallbackQuery, state: FSMContext, **_):
    await state.set_state(Admin.kb_forget)
    await show(call, "🗑 Пришли номер записи (например <code>#12</code> или <code>12</code>) — "
                     "ИИ её забудет.", kb([[("⬅️ Отмена", "a:kb")]]))


@router.message(StateFilter(Admin.kb_forget))
async def kb_forget_input(message: Message, storage, state: FSMContext, **_):
    await state.clear()
    raw = (message.text or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Нужен номер записи.", reply_markup=KB_KB)
        return
    ok = storage.forget(int(raw))
    await message.answer("Забыто ✅" if ok else "Такой записи нет.", reply_markup=KB_KB)


@router.callback_query(F.data == "a:kb:wipe")
async def cb_kb_wipe(call: CallbackQuery, **_):
    await show(call, "⚠️ <b>Стереть всю выученную базу знаний?</b>\n"
                     "ИИ забудет всё и начнёт учиться заново. Это необратимо.",
               kb([[("💣 Да, стереть", "a:kb:wipe:yes"), ("⬅️ Отмена", "a:kb")]]))


@router.callback_query(F.data == "a:kb:wipe:yes")
async def cb_kb_wipe_yes(call: CallbackQuery, storage, **_):
    count = storage.wipe_knowledge()
    storage.log("warn", "kb", "админ стёр базу знаний (%d записей)" % count)
    await show(call, "💣 Память очищена: удалено <b>%d</b> записей.\n\n%s"
               % (count, kb_view(storage)), KB_KB)


# ---------------------------------------------------------------- состояние
@router.callback_query(F.data == "a:health")
async def cb_health(call: CallbackQuery, storage, teacher, autolearner, **_):
    ok, detail = await teacher.health()
    events = storage.recent_events(10)
    lines = [
        "<b>🩺 Состояние системы</b>", "",
        "Учитель: <b>%s</b>" % html.escape(teacher.title),
        "Статус: %s — %s" % ("✅ на связи" if ok else "❌ недоступен", html.escape(detail)),
        "Режим: <code>%s</code>" % config.TEACHER,
        "Аптайм: %s" % human_time(time.time() - storage.started_at),
        "Самообучение: %s" % ("работает" if autolearner and autolearner.running
                              and storage.flag("autolearn", False) else "выключено"),
        "База: <code>%s</code>" % html.escape(str(config.DB_PATH.name)),
        "Полнотекстовый поиск: %s" % ("да" if storage.fts else "нет"),
        "", "<b>Последние события</b>",
    ]
    icons = {"error": "❌", "warn": "⚠️", "info": "ℹ️"}
    if not events:
        lines.append("— чисто, событий нет")
    for ev in events:
        when = time.strftime("%d.%m %H:%M", time.localtime(ev["ts"]))
        lines.append("%s <code>%s</code> %s" % (icons.get(ev["level"], "•"), when,
                                                html.escape((ev["detail"] or ev["kind"])[:110])))
    await show(call, "\n".join(lines), BACK_KB)


# ---------------------------------------------------------------- настройки
def cfg_view(storage) -> str:
    return (
        "<b>⚙️ Настройки ИИ</b>\n\n"
        "<b>Обучение</b>: %s\n<i>запоминать ответы учителя</i>\n\n"
        "<b>Сначала своя память</b>: %s\n<i>если знает ответ — не идёт к учителю</i>\n\n"
        "<b>Самообучение</b>: %s\n<i>сам задаёт учителю вопросы в фоне</i>\n\n"
        "<b>Порог узнавания</b>: <b>%.2f</b>\n"
        "<i>выше — строже, ответ из памяти только на почти такой же вопрос</i>"
    ) % (
        "✅ вкл" if storage.flag("learning") else "❌ выкл",
        "✅ вкл" if storage.flag("brain_first") else "❌ выкл",
        "✅ вкл" if storage.flag("autolearn", False) else "❌ выкл",
        storage.threshold(),
    )


CFG_KB = kb([
    [("🔁 Обучение", "a:cfg:learning"), ("🔁 Своя память", "a:cfg:brain_first")],
    [("🔁 Самообучение", "a:cfg:autolearn")],
    [("➖ порог", "a:cfg:th:-"), ("➕ порог", "a:cfg:th:+")],
    [("⬅️ Назад", "a:home")],
])


@router.callback_query(F.data == "a:cfg")
async def cb_cfg(call: CallbackQuery, storage, **_):
    await show(call, cfg_view(storage), CFG_KB)


@router.callback_query(F.data.startswith("a:cfg:"))
async def cb_cfg_change(call: CallbackQuery, storage, autolearner, **_):
    action = call.data.split(":")[2]
    if action in ("learning", "brain_first", "autolearn"):
        storage.toggle(action)
        if action == "autolearn" and autolearner:
            autolearner.start()
    elif action == "th":
        step = 0.02 if call.data.endswith("+") else -0.02
        storage.set_setting("threshold", "%.2f" % min(0.99, max(0.5, storage.threshold() + step)))
    await show(call, cfg_view(storage), CFG_KB)


# ---------------------------------------------------------------- рассылка
@router.callback_query(F.data == "a:cast")
async def cb_cast(call: CallbackQuery, state: FSMContext, storage, **_):
    await state.set_state(Admin.broadcast)
    total = len(storage.all_user_ids())
    await show(call, "📢 Пришли текст рассылки — покажу предпросмотр.\n"
                     "Получателей: <b>%d</b>" % total, kb([[("⬅️ Отмена", "a:home")]]))


@router.message(StateFilter(Admin.broadcast))
async def cast_input(message: Message, state: FSMContext, storage, **_):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Нужен текст.")
        return
    await state.update_data(text=text)
    await state.set_state(Admin.confirm)
    await message.answer(
        "<b>Предпросмотр</b>\n\n%s\n\n<i>Отправить %d пользователям?</i>"
        % (html.escape(text), len(storage.all_user_ids())),
        reply_markup=kb([[("🚀 Отправить", "a:cast:go"), ("⬅️ Отмена", "a:home")]]),
    )


@router.callback_query(F.data == "a:cast:go", StateFilter(Admin.confirm))
async def cast_go(call: CallbackQuery, state: FSMContext, storage, **_):
    data = await state.get_data()
    await state.clear()
    text = data.get("text") or ""
    targets = storage.all_user_ids()
    await show(call, "🚀 Рассылаю… 0/%d" % len(targets), None)

    sent = failed = 0
    for i, user_id in enumerate(targets, 1):
        try:
            await call.bot.send_message(user_id, html.escape(text))
            sent += 1
        except Exception:
            failed += 1
        if i % 25 == 0:
            await asyncio.sleep(1)
            with contextlib.suppress(Exception):
                await call.message.edit_text("🚀 Рассылаю… %d/%d" % (i, len(targets)))
        else:
            await asyncio.sleep(0.05)

    storage.log("info", "broadcast", "отправлено %d, ошибок %d" % (sent, failed))
    await show(call, "📢 <b>Рассылка завершена</b>\n\nДоставлено: <b>%d</b>\nНе дошло: %d"
               % (sent, failed), MAIN_KB)
