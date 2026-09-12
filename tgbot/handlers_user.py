"""Пользовательская часть: просто чат — текст, фото, голосовые, файлы, видео."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import config
from ai.media import MediaBox, MediaItem, compose_question, guess_mime
from tgbot.format import to_telegram

log = logging.getLogger(__name__)
router = Router(name="user")

GREETING = (
    "Привет! Я <b>{name}</b> — живой ИИ, который учится в процессе разговора.\n\n"
    "Просто пиши мне как обычному собеседнику: вопрос, задача, идея, что угодно. "
    "Можно прислать <b>фото, голосовое, видео или файл</b> — я посмотрю и отвечу "
    "по существу. Помню наш диалог, поэтому мысль можно продолжать.\n\n"
    "Если захочешь начать с чистого листа — /clear."
)

TOO_BIG = ("😕 Файл больше %d МБ — Telegram не отдаёт такие ботам. "
           "Пришли версию поменьше.")
UNSUPPORTED = "😕 Такой формат я прочитать не смогу. Опиши словами, что нужно."
NOT_MEDIA = "Я понимаю текст, фото, голосовые, видео и файлы. Пришли что-нибудь из этого."

# буфер альбомов: media_group_id -> {"items": [...], "timer": Task}
_albums: Dict[str, dict] = {}


async def _keep_typing(message: Message) -> None:
    """Держим статус «печатает», пока идёт долгий поход к учителю."""
    while True:
        with contextlib.suppress(Exception):
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        await asyncio.sleep(4)


# ---------------------------------------------------------------- разбор вложений
def describe_attachment(message: Message) -> Optional[Tuple[str, str, str, str, int]]:
    """Достаёт из сообщения (file_id, вид, имя, mime, размер)."""
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, "photo", "photo.jpg", "image/jpeg", photo.file_size or 0
    if message.sticker:
        st = message.sticker
        if st.is_animated:  # .tgs — это векторная анимация, Gemini её не читает
            return None
        if st.is_video:
            return st.file_id, "sticker", "sticker.webm", "video/webm", st.file_size or 0
        return st.file_id, "sticker", "sticker.webp", "image/webp", st.file_size or 0
    if message.voice:
        v = message.voice
        return v.file_id, "voice", "voice.ogg", v.mime_type or "audio/ogg", v.file_size or 0
    if message.audio:
        a = message.audio
        name = a.file_name or "audio.mp3"
        return a.file_id, "audio", name, a.mime_type or guess_mime(name), a.file_size or 0
    if message.video:
        v = message.video
        name = v.file_name or "video.mp4"
        return v.file_id, "video", name, v.mime_type or "video/mp4", v.file_size or 0
    if message.video_note:
        v = message.video_note
        return v.file_id, "video_note", "circle.mp4", "video/mp4", v.file_size or 0
    if message.animation:
        a = message.animation
        name = a.file_name or "animation.mp4"
        return a.file_id, "video", name, a.mime_type or "video/mp4", a.file_size or 0
    if message.document:
        d = message.document
        name = d.file_name or "file"
        return d.file_id, "document", name, d.mime_type or guess_mime(name), d.file_size or 0
    return None


async def _download(bot: Bot, message: Message, box: MediaBox) -> Optional[MediaItem]:
    info = describe_attachment(message)
    if info is None:
        return None
    file_id, kind, name, mime, size = info
    if size > config.MAX_MEDIA_MB * 1024 * 1024:
        raise ValueError("too_big")
    path = box.path_for(name)
    await bot.download(file_id, destination=str(path))
    return MediaItem(path, guess_mime(name, mime), kind, name, size)


# ---------------------------------------------------------------- общий ответ
async def _answer(message: Message, storage, brain,
                  media: List[MediaItem], caption: str) -> None:
    user = message.from_user
    question = compose_question(caption, media)

    if brain.busy(user.id):
        await message.answer("⏳ Секунду, я ещё думаю над прошлым сообщением.")
        return

    typing = asyncio.create_task(_keep_typing(message))
    try:
        async with brain.lock(user.id):
            result = await brain.respond(user.id, question, media)
    finally:
        typing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await typing

    storage.bump_user(user.id)
    for chunk in to_telegram(result.text):
        try:
            await message.answer(chunk, disable_web_page_preview=True)
        except Exception as exc:  # разметка не зашла — шлём как есть
            log.warning("Ошибка отправки: %s", exc)
            await message.answer(chunk, parse_mode=None, disable_web_page_preview=True)


# ---------------------------------------------------------------- команды
@router.message(CommandStart())
async def on_start(message: Message, storage, **_) -> None:
    storage.upsert_user(message.from_user)
    await message.answer(GREETING.format(name=config.BOT_NAME))


@router.message(Command("clear"))
async def on_clear(message: Message, storage, **_) -> None:
    storage.upsert_user(message.from_user)
    removed = storage.clear_context(message.from_user.id)
    if removed:
        await message.answer(
            "🧹 Контекст очищен — забыл, о чём мы говорили (%d реплик).\n"
            "Но всё, чему научился, осталось при мне." % removed
        )
    else:
        await message.answer("Контекст и так пуст — можем начинать с чистого листа.")


# ---------------------------------------------------------------- текст
@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, storage, brain, **_) -> None:
    storage.upsert_user(message.from_user)
    if storage.is_blocked(message.from_user.id):
        return

    text = (message.text or "").strip()
    if len(text) > config.MAX_INPUT_CHARS:
        await message.answer(
            "Слишком длинное сообщение — сократи примерно до %d символов."
            % config.MAX_INPUT_CHARS
        )
        return
    await _answer(message, storage, brain, [], text)


# ---------------------------------------------------------------- медиа
MEDIA_FILTER = (
    F.photo | F.voice | F.audio | F.video | F.video_note
    | F.document | F.animation | F.sticker
)


@router.message(MEDIA_FILTER)
async def on_media(message: Message, storage, brain, **_) -> None:
    storage.upsert_user(message.from_user)
    if storage.is_blocked(message.from_user.id):
        return

    if message.media_group_id:
        await _collect_album(message, storage, brain)
        return

    if describe_attachment(message) is None:
        await message.answer(UNSUPPORTED)
        return

    box = MediaBox(config.MEDIA_DIR)
    try:
        try:
            item = await _download(message.bot, message, box)
        except ValueError:
            await message.answer(TOO_BIG % config.MAX_MEDIA_MB)
            return
        except Exception as exc:
            log.warning("Не скачался файл: %s", exc)
            storage.log("warn", "download", str(exc))
            await message.answer("😕 Не смог забрать файл из Telegram. Попробуй ещё раз.")
            return
        if item is None:
            await message.answer(UNSUPPORTED)
            return
        await _answer(message, storage, brain, [item], message.caption or "")
    finally:
        box.cleanup()


async def _collect_album(message: Message, storage, brain) -> None:
    """Несколько файлов одним сообщением приходят по одному — собираем их вместе."""
    group = message.media_group_id
    album = _albums.setdefault(group, {"items": [], "timer": None})
    album["items"].append(message)

    if album["timer"] is not None:
        album["timer"].cancel()
    album["timer"] = asyncio.create_task(_flush_album(group, storage, brain))


async def _flush_album(group: str, storage, brain) -> None:
    try:
        await asyncio.sleep(config.ALBUM_WAIT)
    except asyncio.CancelledError:
        return  # пришёл ещё файл — ждём заново

    album = _albums.pop(group, None)
    if not album or not album["items"]:
        return

    messages: List[Message] = album["items"][: config.MAX_ALBUM]
    head = messages[0]
    caption = next((m.caption for m in album["items"] if m.caption), "") or ""

    box = MediaBox(config.MEDIA_DIR)
    try:
        items: List[MediaItem] = []
        oversized = False
        for msg in messages:
            try:
                item = await _download(msg.bot, msg, box)
            except ValueError:
                oversized = True
                continue
            except Exception as exc:
                log.warning("Файл из альбома не скачался: %s", exc)
                continue
            if item is not None:
                items.append(item)

        if not items:
            await head.answer(TOO_BIG % config.MAX_MEDIA_MB if oversized else UNSUPPORTED)
            return
        await _answer(head, storage, brain, items, caption)
    finally:
        box.cleanup()


# ---------------------------------------------------------------- остальное
@router.message()
async def on_other(message: Message, storage, **_) -> None:
    storage.upsert_user(message.from_user)
    await message.answer(NOT_MEDIA)
