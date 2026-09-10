"""Горячее переключение веб-учителя Gemini/ChatGPT без перезапуска бота."""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Sequence, Tuple

from ..media import MediaItem
from .base import NeedsLogin, Teacher, TeacherError
from .chatgpt_web import ChatGPTWebTeacher
from .gemini_web import GeminiWebTeacher
from .session_cookies import CookieStore, parse_cookies


class ProviderManager(Teacher):
    def __init__(self, config, storage):
        self.config = config
        self.storage = storage
        self.cookies = CookieStore(config.SESSION_STORE_PATH)
        self._teacher: Optional[Teacher] = None
        self._lock = asyncio.Lock()
        selected = str(storage.get_setting("provider", "gemini") or "gemini").lower()
        self.selected = selected if selected in ("gemini", "chatgpt") else "gemini"

    @property
    def name(self) -> str:
        return self.selected

    @property
    def title(self) -> str:
        if self._teacher:
            return self._teacher.title
        return "Gemini · gemini.google.com" if self.selected == "gemini" else "ChatGPT · chatgpt.com"

    def has_cookies(self, provider: str) -> bool:
        return self.cookies.has(provider)

    def _url_for(self, provider: str) -> str:
        return self.config.GEMINI_WEB_URL if provider == "gemini" else self.config.CHATGPT_WEB_URL

    def _make(self, provider: str) -> Teacher:
        cookies = self.cookies.get(provider)
        if provider == "gemini":
            return GeminiWebTeacher(
                url=self.config.GEMINI_WEB_URL,
                profile_dir=self.config.GEMINI_PROFILE_DIR,
                bot_name=self.config.BOT_NAME,
                cookies=cookies,
                headless=self.config.WEB_HEADLESS,
                timeout=self.config.GEMINI_WEB_TIMEOUT,
                new_chat=self.config.WEB_NEW_CHAT,
            )
        if provider == "chatgpt":
            return ChatGPTWebTeacher(
                url=self.config.CHATGPT_WEB_URL,
                profile_dir=self.config.CHATGPT_PROFILE_DIR,
                bot_name=self.config.BOT_NAME,
                cookies=cookies,
                headless=self.config.WEB_HEADLESS,
                timeout=self.config.CHATGPT_WEB_TIMEOUT,
                new_chat=self.config.WEB_NEW_CHAT,
            )
        raise TeacherError("Неизвестный провайдер: %s" % provider)

    async def start(self) -> None:
        async with self._lock:
            if self._teacher:
                return
            teacher = self._make(self.selected)
            await teacher.start()
            ok, detail = await teacher.health()
            if not ok:
                await teacher.close()
                raise NeedsLogin(detail)
            self._teacher = teacher

    async def close(self) -> None:
        async with self._lock:
            old, self._teacher = self._teacher, None
            if old:
                await old.close()

    async def switch(self, provider: str, cookie_text: str) -> Tuple[bool, str]:
        provider = provider.lower().strip()
        if provider not in ("gemini", "chatgpt"):
            return False, "Неизвестный провайдер"
        parsed = parse_cookies(cookie_text, self._url_for(provider))
        self.cookies.set(provider, parsed)

        async with self._lock:
            candidate = self._make(provider)
            try:
                await candidate.start()
                ok, detail = await candidate.health()
                if not ok:
                    raise NeedsLogin(detail)
            except Exception as exc:
                try:
                    await candidate.close()
                except Exception:
                    pass
                self.cookies.clear(provider)
                return False, str(exc)

            old = self._teacher
            self._teacher = candidate
            self.selected = provider
            self.storage.set_setting("provider", provider)
            self.storage.log("info", "provider", "переключён на %s" % provider)
            if old:
                try:
                    await old.close()
                except Exception:
                    pass
            return True, detail

    async def ask(self, question: str, context: List[Dict[str, str]],
                  media: Optional[Sequence[MediaItem]] = None) -> str:
        if not self._teacher:
            await self.start()
        async with self._lock:
            if not self._teacher:
                raise TeacherError("Источник знаний не запущен")
            return await self._teacher.ask(question, context, media)

    async def health(self) -> Tuple[bool, str]:
        if not self._teacher:
            if not self.has_cookies(self.selected):
                return False, "cookies не заданы — открой /admin → Настройки"
            return False, "провайдер ещё не запущен"
        return await self._teacher.health()
