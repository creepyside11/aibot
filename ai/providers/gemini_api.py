"""Учитель через официальный бесплатный Gemini API (запасной режим)."""
from __future__ import annotations

import asyncio
import base64
import logging
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp

from .. import prompt as prompt_mod
from ..media import MediaItem
from .base import MediaUnsupported, NeedsLogin, Teacher, TeacherError

log = logging.getLogger(__name__)
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiApiTeacher(Teacher):
    name = "api"
    title = "Gemini API (бесплатный)"

    def __init__(self, api_key: str, model: str, bot_name: str, timeout: int = 120):
        self.api_key = api_key
        self.model = model
        self.bot_name = bot_name
        self.timeout = timeout
        self._session = None
        self._files_sent = 0

    async def start(self) -> None:
        if not self.api_key:
            raise NeedsLogin(
                "Не задан GEMINI_API_KEY. Возьми бесплатный ключ на "
                "https://aistudio.google.com/apikey и впиши его в .env"
            )
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @staticmethod
    def _media_parts(media: Optional[Sequence[MediaItem]]) -> List[Dict]:
        """Файлы уходят прямо в запрос — base64 внутри inline_data."""
        parts: List[Dict] = []
        for item in media or []:
            if not item.api_ready:
                raise MediaUnsupported(
                    "Gemini API не читает файлы типа %s" % item.mime)
            if item.size > 18 * 1024 * 1024:
                raise MediaUnsupported("Файл слишком большой для Gemini API")
            try:
                raw = item.path.read_bytes()
            except OSError as exc:
                raise TeacherError("Не смог прочитать файл: %s" % exc)
            parts.append({
                "inline_data": {
                    "mime_type": item.mime,
                    "data": base64.b64encode(raw).decode("ascii"),
                }
            })
        return parts

    async def ask(self, question: str, context: List[Dict[str, str]],
                  media: Optional[Sequence[MediaItem]] = None) -> str:
        if self._session is None:
            await self.start()

        media_parts = self._media_parts(media)
        payload = {
            "system_instruction": {
                "parts": [{"text": prompt_mod.system_prompt(self.bot_name)}]
            },
            "contents": prompt_mod.build_api_contents(question, context, media_parts),
            "generationConfig": {"temperature": 0.9, "maxOutputTokens": 2048},
        }
        url = ENDPOINT.format(model=self.model)
        headers = {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

        last_error = "неизвестная ошибка"
        for attempt in range(3):
            try:
                async with self._session.post(url, json=payload, headers=headers) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 200:
                        text = self._extract(data)
                        if text:
                            self._files_sent += len(media_parts)
                            return text
                        last_error = "пустой ответ модели"
                    elif resp.status in (429, 500, 502, 503, 504):
                        last_error = "код %s" % resp.status
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    else:
                        message = (data.get("error") or {}).get("message", str(data)[:300])
                        if resp.status in (401, 403):
                            raise NeedsLogin("Ключ Gemini API отклонён: %s" % message)
                        raise TeacherError("Gemini API %s: %s" % (resp.status, message))
            except aiohttp.ClientError as exc:
                last_error = str(exc)
                await asyncio.sleep(1.5 * (attempt + 1))
            except asyncio.TimeoutError:
                last_error = "таймаут ожидания ответа"
        raise TeacherError("Gemini не ответил: %s" % last_error)

    @staticmethod
    def _extract(data: Dict) -> str:
        candidates = data.get("candidates") or []
        chunks = []
        for cand in candidates:
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("text"):
                    chunks.append(part["text"])
            if chunks:
                break
        return "\n".join(chunks).strip()

    async def health(self) -> Tuple[bool, str]:
        if not self.api_key:
            return False, "нет ключа GEMINI_API_KEY"
        return True, "модель %s · файлов отправлено %d" % (self.model, self._files_sent)
