"""Общий интерфейс учителя (источника знаний)."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple


class TeacherError(Exception):
    """Учитель временно недоступен или не понял запрос."""


class MediaUnsupported(TeacherError):
    """Учитель не умеет читать такой файл."""


class MediaAttachError(TeacherError):
    """Файл не удалось положить в чат — отправлять сообщение бессмысленно."""


class NeedsLogin(TeacherError):
    """Нужен ручной вход в аккаунт Google."""


class Teacher:
    name = "teacher"
    title = "Учитель"

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def ask(self, question: str, context: List[Dict[str, str]],
                  media: Optional[Sequence] = None) -> str:
        """media — список MediaItem, прикреплённых к сообщению."""
        raise NotImplementedError

    async def health(self) -> Tuple[bool, str]:
        return True, "ok"
