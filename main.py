"""Точка входа: Telegram-ИИ с памятью и веб-учителями Gemini/ChatGPT."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

import config
from ai.autolearn import AutoLearner
from ai.brain import Brain
from ai.media import sweep
from ai.providers import build_teacher
from ai.providers.base import TeacherError
from ai.storage import Storage
from tgbot import admin_provider, handlers_admin, handlers_user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
log = logging.getLogger("main")

USER_COMMANDS = [
    BotCommand(command="start", description="Начать общение"),
    BotCommand(command="clear", description="Очистить контекст диалога"),
]
ADMIN_COMMANDS = USER_COMMANDS + [
    BotCommand(command="admin", description="Панель управления"),
]


def make_alerter(bot: Bot):
    """Сообщает админу о поломках, но не чаще раза в 10 минут."""
    state = {"last": 0.0}

    async def alert(text: str) -> None:
        now = time.time()
        if not config.ADMIN_ID or now - state["last"] < 600:
            return
        state["last"] = now
        with contextlib.suppress(Exception):
            await bot.send_message(config.ADMIN_ID, "🚨 <b>Внимание</b>\n%s" % text)

    return alert


async def main() -> None:
    if not config.BOT_TOKEN:
        sys.exit("Не задан BOT_TOKEN в .env")

    sweep(config.MEDIA_DIR)  # чистим хвосты, если прошлый запуск оборвался
    storage = Storage(config.DB_PATH, config.SIM_THRESHOLD)
    teacher = build_teacher(config, storage)
    brain = Brain(storage, teacher, config.CONTEXT_TURNS)
    autolearner = AutoLearner(brain)

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    brain.on_alert = make_alerter(bot)

    # storage в конструкторе — это FSM-хранилище aiogram, поэтому свои
    # объекты кладём в workflow_data: они прилетят в хендлеры как аргументы
    dp = Dispatcher(storage=MemoryStorage())
    dp["storage"] = storage
    dp["brain"] = brain
    dp["teacher"] = teacher
    dp["autolearner"] = autolearner
    dp.include_router(admin_provider.router)
    dp.include_router(handlers_admin.router)
    dp.include_router(handlers_user.router)

    me = await bot.get_me()
    log.info("Бот @%s запущен, админ: %s", me.username, config.ADMIN_ID)

    await bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    if config.ADMIN_ID:
        with contextlib.suppress(Exception):
            await bot.set_my_commands(ADMIN_COMMANDS,
                                      scope=BotCommandScopeChat(chat_id=config.ADMIN_ID))

    log.info("Поднимаю учителя: %s", teacher.title)
    try:
        await teacher.start()
        storage.log("info", "start", "учитель готов: %s" % teacher.title)
        log.info("Учитель готов")
    except Exception as exc:
        storage.log("error", "start", str(exc))
        log.error("Учитель не поднялся: %s", exc)
        if config.ADMIN_ID:
            with contextlib.suppress(Exception):
                await bot.send_message(
                    config.ADMIN_ID,
                    "🚨 Источник знаний не запустился:\n<code>%s</code>\n\n"
                    "Бот работает, но отвечать сможет только из уже выученного."
                    % str(exc)[:500],
                )

    if storage.flag("autolearn", False):
        autolearner.start()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        log.info("Останавливаюсь…")
        await autolearner.stop()
        with contextlib.suppress(Exception):
            await teacher.close()
        storage.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
