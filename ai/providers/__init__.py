"""Учителя — источники знаний для моего ИИ."""
from __future__ import annotations

from .base import NeedsLogin, Teacher, TeacherError
from .gemini_api import GeminiApiTeacher
from .gemini_web import GeminiWebTeacher

__all__ = ["Teacher", "TeacherError", "NeedsLogin", "GeminiApiTeacher", "GeminiWebTeacher"]


def build_teacher(config):
    """Собирает учителя по настройкам из .env."""
    if config.TEACHER == "api":
        return GeminiApiTeacher(
            api_key=config.GEMINI_API_KEY,
            model=config.GEMINI_MODEL,
            bot_name=config.BOT_NAME,
            timeout=config.GEMINI_API_TIMEOUT,
        )
    return GeminiWebTeacher(
        url=config.GEMINI_WEB_URL,
        profile_dir=config.GEMINI_PROFILE_DIR,
        bot_name=config.BOT_NAME,
        headless=config.GEMINI_WEB_HEADLESS,
        timeout=config.GEMINI_WEB_TIMEOUT,
        new_chat=config.GEMINI_WEB_NEW_CHAT,
    )
