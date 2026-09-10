"""Источники знаний и горячее переключение между ними."""
from .base import NeedsLogin, Teacher, TeacherError
from .chatgpt_web import ChatGPTWebTeacher
from .gemini_web import GeminiWebTeacher
from .manager import ProviderManager

__all__ = [
    "Teacher", "TeacherError", "NeedsLogin", "GeminiWebTeacher",
    "ChatGPTWebTeacher", "ProviderManager", "build_teacher",
]


def build_teacher(config, storage):
    return ProviderManager(config, storage)
