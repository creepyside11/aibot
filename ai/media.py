"""Медиа из Telegram: скачивание, типы и то, как о них спросить учителя."""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path
from typing import List, Optional

# что Gemini умеет читать: расширение -> mime
EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
    ".heif": "image/heif", ".bmp": "image/bmp",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/ogg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mpeg": "video/mpeg", ".avi": "video/x-msvideo", ".mkv": "video/x-matroska",
    ".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown",
    ".csv": "text/csv", ".json": "application/json", ".xml": "text/xml",
    ".html": "text/html", ".py": "text/x-python", ".js": "text/javascript",
    ".rtf": "text/rtf",
}

# что примет Gemini API (режим api). В браузерном чате список шире.
API_MIME_PREFIXES = ("image/", "audio/", "video/")
API_MIME_EXACT = {
    "application/pdf", "text/plain", "text/markdown", "text/csv",
    "application/json", "text/xml", "text/html", "text/x-python",
    "text/javascript", "text/rtf",
}

# как ИИ формулирует просьбу учителю, если пользователь не написал подпись
DEFAULT_ASK = {
    "photo": "Посмотри на это изображение и опиши по существу то, что важно.",
    "sticker": "Это стикер от собеседника. Отреагируй живо и коротко.",
    "voice": "Это голосовое сообщение собеседника. Послушай и ответь на него "
             "так, будто он написал это текстом.",
    "audio": "Послушай эту аудиозапись и расскажи, что в ней.",
    "video": "Посмотри это видео и расскажи, что в нём происходит.",
    "video_note": "Это кружок-видеосообщение. Посмотри и ответь на него.",
    "document": "Изучи этот файл и объясни главное по его содержимому.",
}

KIND_LABEL = {
    "photo": "фото", "sticker": "стикер", "voice": "голосовое",
    "audio": "аудио", "video": "видео", "video_note": "видеосообщение",
    "document": "файл",
}


class MediaItem:
    """Один скачанный файл, готовый уйти в чат к учителю."""

    __slots__ = ("path", "mime", "kind", "name", "size")

    def __init__(self, path: Path, mime: str, kind: str, name: str, size: int = 0):
        self.path = Path(path)
        self.mime = mime
        self.kind = kind
        self.name = name
        self.size = size or (self.path.stat().st_size if self.path.exists() else 0)

    @property
    def label(self) -> str:
        return KIND_LABEL.get(self.kind, "файл")

    @property
    def api_ready(self) -> bool:
        if self.mime.startswith(API_MIME_PREFIXES):
            return True
        return self.mime in API_MIME_EXACT

    def __repr__(self) -> str:
        return "<MediaItem %s %s %.0fКБ>" % (self.kind, self.mime, self.size / 1024.0)


def guess_mime(name: str, fallback: Optional[str] = None) -> str:
    ext = Path(name or "").suffix.lower()
    if ext in EXT_MIME:
        return EXT_MIME[ext]
    return fallback or "application/octet-stream"


def describe(media: List[MediaItem]) -> str:
    """Короткая пометка для контекста диалога и журнала: «[фото]», «[2 файла]»."""
    if not media:
        return ""
    if len(media) == 1:
        return "[%s]" % media[0].label
    kinds = set(item.label for item in media)
    if len(kinds) == 1:
        return "[%d %s]" % (len(media), kinds.pop())
    return "[%d файла]" % len(media)


def default_question(media: List[MediaItem]) -> str:
    if not media:
        return ""
    if len(media) > 1:
        return "Посмотри на эти файлы и ответь по существу того, что в них."
    return DEFAULT_ASK.get(media[0].kind, DEFAULT_ASK["document"])


def compose_question(caption: str, media: List[MediaItem]) -> str:
    """Подпись пользователя главнее — она и есть вопрос."""
    caption = (caption or "").strip()
    if caption:
        return caption
    return default_question(media)


class MediaBox:
    """Временная папка под скачанные файлы: сама за собой убирает."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / ("%d_%s" % (int(time.time()), uuid.uuid4().hex[:8]))
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str) -> Path:
        safe = "".join(c for c in (name or "file") if c.isalnum() or c in "._- ")[-60:]
        return self.dir / (safe or "file")

    def cleanup(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
        return False


def sweep(root: Path, older_than_sec: int = 3600) -> None:
    """Подчищает мусор, если бота убили на середине обработки."""
    root = Path(root)
    if not root.exists():
        return
    now = time.time()
    for child in root.iterdir():
        try:
            if child.is_dir() and now - child.stat().st_mtime > older_than_sec:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue
