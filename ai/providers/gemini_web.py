"""Gemini web teacher authenticated with cookies supplied from Telegram admin."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .. import prompt as prompt_mod
from ..media import MediaItem
from .base import MediaAttachError, NeedsLogin, Teacher, TeacherError

log = logging.getLogger(__name__)

EDITOR_SELECTORS = (
    'rich-textarea div.ql-editor[contenteditable="true"]',
    'div.ql-editor[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'textarea[aria-label]',
)
SEND_SELECTORS = (
    'button.send-button:not([disabled])',
    'button[aria-label*="Отправить"]:not([disabled])',
    'button[aria-label*="Send"]:not([disabled])',
)
RESPONSE_SELECTORS = (
    'message-content.model-response-text',
    '.model-response-text .markdown',
    '.model-response-text',
    'model-response .markdown',
    'model-response',
)
STOP_SELECTORS = (
    'button[aria-label*="Остановить"]',
    'button[aria-label*="Stop"]',
    'button.stop-button',
)
NEW_CHAT_SELECTORS = (
    '[data-test-id="new-chat-button"]',
    'button[aria-label*="Новый чат"]',
    'button[aria-label*="New chat"]',
)
ATTACH_SELECTORS = (
    'button[aria-label="Загрузка и инструменты"]',
    'button[aria-label*="Загрузка и инструмент"]',
    'button[aria-label*="Upload and tools"]',
    'button[aria-label*="Добавить файл"]',
    'button[aria-label*="Add files"]',
    'button[aria-label*="Attach"]',
)
FILE_INPUT = 'input.hidden-file-input, input[type="file"]'
PREVIEW = 'uploader-file-preview, gem-media-attachment, .file-preview-chip, .attachment-preview-wrapper'
LOGIN_MARKERS = ("accounts.google.com", "servicelogin", "signin")
JUNK_LINES = {
    "ответ gemini", "gemini", "ответ", "показать детали", "показать ход мыслей",
    "show thinking", "show details", "copy", "share", "копировать", "поделиться",
}
JUNK_PREFIX = re.compile(r"^\s*(ответ\s+gemini|gemini'?s?\s+response|gemini|ответ)\s*[:\-—]\s*", re.I)


class GeminiWebTeacher(Teacher):
    name = "gemini"
    title = "Gemini · gemini.google.com"

    def __init__(self, url: str, profile_dir, bot_name: str, cookies=None,
                 headless: bool = True, timeout: int = 180, new_chat: bool = True):
        self.url = url
        self.profile_dir = str(profile_dir)
        self.bot_name = bot_name
        self.cookies = list(cookies or [])
        self.headless = headless
        self.timeout = timeout
        self.new_chat = new_chat
        self._pw = self._ctx = self._page = None
        self._lock = asyncio.Lock()
        self._asked = 0
        self._files_sent = 0
        self._last_way = ""

    async def start(self) -> None:
        if not self.cookies:
            raise NeedsLogin("Для Gemini не заданы cookies. Открой /admin → Настройки → Gemini.")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise TeacherError("Не установлен Playwright/Chromium.") from exc

        self._pw = await async_playwright().start()
        kwargs = dict(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception:
            self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        await self._ctx.add_cookies(self.cookies)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.set_default_timeout(30000)
        await self._open_chat(force=True)

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._pw = self._ctx = self._page = None

    async def _alive(self) -> bool:
        return bool(self._page and not self._page.is_closed())

    async def _ensure(self) -> None:
        if not await self._alive():
            await self.close()
            await self.start()

    async def _first(self, selectors, timeout: int = 1200):
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                await loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception:
                pass
        return None

    async def _page_state(self) -> dict:
        try:
            return await self._page.evaluate("""() => {
                const dialogs = [...document.querySelectorAll('[role="dialog"],mat-dialog-container')];
                const consent = dialogs.some(d => /cookie|файлы cookie|Before you continue|Прежде чем перейти/i.test(d.innerText || ''));
                const signIn = [...document.querySelectorAll('a,button')].some(el => /^(войти|sign in)$/i.test((el.innerText || '').trim()));
                const composer = !!document.querySelector('rich-textarea div.ql-editor[contenteditable="true"],div.ql-editor[contenteditable="true"],div[contenteditable="true"][role="textbox"]');
                return {consent, signIn, composer};
            }""")
        except Exception:
            return {"consent": False, "signIn": False, "composer": False}

    async def _check_login(self) -> None:
        url = (self._page.url or "").lower()
        if any(x in url for x in LOGIN_MARKERS):
            raise NeedsLogin("Cookies Gemini истекли. Обнови их в /admin → Настройки → Gemini.")
        state = await self._page_state()
        if state.get("consent") or (state.get("signIn") and not state.get("composer")):
            raise NeedsLogin("Gemini не принял cookies. Обнови их в админке.")

    async def _open_chat(self, force: bool = False) -> None:
        if self.new_chat and not force:
            btn = await self._first(NEW_CHAT_SELECTORS, 700)
            if btn:
                with contextlib.suppress(Exception):
                    await btn.click()
                    await self._page.wait_for_timeout(500)
                    await self._check_login()
                    return
        if force or self.new_chat:
            try:
                await self._page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise TeacherError("Не удалось открыть Gemini: %s" % exc)
            await self._page.wait_for_timeout(1500)
        await self._check_login()

    async def _attachment_count(self) -> int:
        try:
            return await self._page.locator(PREVIEW).count()
        except Exception:
            return 0

    async def _wait_attached(self, before: int) -> bool:
        for _ in range(30):
            if await self._attachment_count() > before:
                await self._page.wait_for_timeout(900)
                return True
            await self._page.wait_for_timeout(250)
        return False

    async def _attach(self, media: Sequence[MediaItem]) -> None:
        paths = [str(x.path) for x in media]
        before = await self._attachment_count()

        btn = await self._first(ATTACH_SELECTORS, 1000)
        if btn:
            with contextlib.suppress(Exception):
                await btn.click()
                await self._page.wait_for_timeout(350)

        inputs = self._page.locator(FILE_INPUT)
        try:
            count = await inputs.count()
        except Exception:
            count = 0
        for i in range(count):
            try:
                await inputs.nth(i).set_input_files(paths)
                if await self._wait_attached(before):
                    self._last_way = "file input"
                    return
            except Exception:
                continue

        btn = await self._first(ATTACH_SELECTORS, 1200)
        if btn:
            try:
                async with self._page.expect_file_chooser(timeout=5000) as info:
                    await btn.click()
                chooser = await info.value
                await chooser.set_files(paths)
                if await self._wait_attached(before):
                    self._last_way = "file chooser"
                    return
            except Exception:
                pass
        raise MediaAttachError("Gemini не принял вложение — возможно, изменилась кнопка загрузки")

    async def _write(self, editor, text: str) -> None:
        try:
            await editor.fill(text)
            return
        except Exception:
            pass
        with contextlib.suppress(Exception):
            await editor.click()
            await editor.press("Control+A")
            await editor.press("Backspace")
        for i, line in enumerate(text.split("\n")):
            if i:
                await self._page.keyboard.press("Shift+Enter")
            await self._page.keyboard.type(line, delay=1)

    async def _send(self, editor) -> None:
        button = await self._first(SEND_SELECTORS, 2200)
        if button:
            with contextlib.suppress(Exception):
                await button.click()
                return
        await editor.press("Enter")

    async def _responses(self):
        for sel in RESPONSE_SELECTORS:
            loc = self._page.locator(sel)
            try:
                if await loc.count():
                    return loc
            except Exception:
                pass
        return self._page.locator(RESPONSE_SELECTORS[-1])

    async def _count_responses(self) -> int:
        try:
            return await (await self._responses()).count()
        except Exception:
            return 0

    async def _generating(self) -> bool:
        for sel in STOP_SELECTORS:
            try:
                if await self._page.locator(sel).first.is_visible(timeout=200):
                    return True
            except Exception:
                pass
        return False

    async def _read_last(self) -> str:
        try:
            loc = await self._responses()
            count = await loc.count()
            return ((await loc.nth(count - 1).inner_text()) or "") if count else ""
        except Exception:
            return ""

    async def _wait_answer(self, before: int) -> str:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        while loop.time() < deadline:
            if await self._count_responses() > before or await self._generating():
                break
            await self._page.wait_for_timeout(400)
        else:
            await self._check_login()
            raise TeacherError("Gemini не начал отвечать")

        last, stable = "", None
        while loop.time() < deadline:
            text = await self._read_last()
            if text and text == last:
                stable = stable or loop.time()
                if loop.time() - stable >= 2.5 and not await self._generating():
                    return text.strip()
            else:
                last, stable = text, None
            await self._page.wait_for_timeout(450)
        if last.strip():
            return last.strip()
        raise TeacherError("Истекло время ожидания ответа Gemini")

    @staticmethod
    def _clean(text: str) -> str:
        lines = [x for x in (text or "").splitlines()
                 if x.strip().lower().strip(" .:—-") not in JUNK_LINES]
        result = JUNK_PREFIX.sub("", "\n".join(lines).strip(), count=1)
        return re.sub(r"\n{3,}", "\n\n", result).strip()

    async def ask(self, question: str, context: List[Dict[str, str]],
                  media: Optional[Sequence[MediaItem]] = None) -> str:
        message = prompt_mod.build_web_message(self.bot_name, question, context, media)
        async with self._lock:
            await self._ensure()
            await self._open_chat()
            editor = await self._first(EDITOR_SELECTORS, 15000)
            await self._check_login()
            if editor is None:
                raise TeacherError("Не найдено поле ввода Gemini")
            before = await self._count_responses()
            if media:
                await self._attach(media)
                self._files_sent += len(media)
                editor = await self._first(EDITOR_SELECTORS, 5000) or editor
            await self._write(editor, message)
            await self._send(editor)
            answer = await self._wait_answer(before)
            self._asked += 1
            return self._clean(answer)

    async def health(self) -> Tuple[bool, str]:
        if not await self._alive():
            return False, "браузер не запущен"
        try:
            await self._check_login()
        except NeedsLogin as exc:
            return False, str(exc)
        state = await self._page_state()
        if not state.get("composer"):
            return False, "Gemini не открыл поле ввода — обнови cookies или пройди дополнительную проверку"
        tail = " · загрузка: %s" % self._last_way if self._last_way else ""
        return True, "чат открыт · вопросов %d · файлов %d%s" % (self._asked, self._files_sent, tail)
