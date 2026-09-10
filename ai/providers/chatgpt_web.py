"""Учитель через веб-чат ChatGPT на chatgpt.com с входом по cookies."""
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
    '#prompt-textarea',
    'div#prompt-textarea[contenteditable="true"]',
    'textarea#prompt-textarea',
    'textarea[name="prompt-textarea"]',
    'div[contenteditable="true"][data-virtualkeyboard="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'textarea[placeholder*="Message"]',
    'textarea[placeholder*="Сообщение"]',
)
SEND_SELECTORS = (
    'button#composer-submit-button:not([disabled])',
    'button[data-testid="send-button"]:not([disabled])',
    'button[aria-label*="Send prompt"]:not([disabled])',
    'button[aria-label*="Send"]:not([disabled])',
    'button[aria-label*="Отправ"]:not([disabled])',
)
STOP_SELECTORS = (
    'button[data-testid="stop-button"]',
    'button[aria-label*="Stop generating"]',
    'button[aria-label*="Stop"]',
    'button[aria-label*="Останов"]',
)
RESPONSE_SELECTORS = (
    '[data-message-author-role="assistant"] .markdown',
    '[data-message-author-role="assistant"] [class*="markdown"]',
    '[data-message-author-role="assistant"]',
)
NEW_CHAT_SELECTORS = (
    'a[data-testid="create-new-chat-button"]',
    'button[data-testid="create-new-chat-button"]',
    'a[aria-label*="New chat"]',
    'button[aria-label*="New chat"]',
    'a[href="/"]',
)
ATTACH_BUTTONS = (
    'button[data-testid="composer-plus-btn"]',
    'button[aria-label*="Attach"]',
    'button[aria-label*="Upload"]',
    'button[aria-label*="Прикреп"]',
    'button[aria-label*="Добав"]',
)
PREVIEW_SELECTORS = (
    '[data-testid*="attachment"]',
    '[class*="attachment"]',
    '[class*="file-preview"]',
    'img[src^="blob:"]',
)
LOGIN_MARKERS = ("auth0.openai.com", "/auth/login", "/auth/", "login")


class ChatGPTWebTeacher(Teacher):
    name = "chatgpt"
    title = "ChatGPT · chatgpt.com"

    def __init__(self, url: str, profile_dir, bot_name: str, cookies: Sequence[dict],
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

    async def start(self) -> None:
        if not self.cookies:
            raise NeedsLogin("Для ChatGPT не заданы cookies. Открой /admin → Настройки → ChatGPT.")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise TeacherError("Не установлен Playwright/Chromium") from exc
        self._pw = await async_playwright().start()
        kwargs = dict(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception:
            self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        await self._ctx.add_cookies(self.cookies)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.set_default_timeout(30000)
        await self._open_chat(force=True)
        await self._check_login()

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

    async def _first(self, selectors, timeout: int = 1500):
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                await loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception:
                continue
        return None

    async def _check_login(self) -> None:
        url = (self._page.url or "").lower()
        editor = await self._first(EDITOR_SELECTORS, timeout=1200)
        if editor is not None:
            return
        if any(marker in url for marker in LOGIN_MARKERS):
            raise NeedsLogin("Cookies ChatGPT истекли. Обнови их в /admin → Настройки → ChatGPT.")
        try:
            login_visible = await self._page.locator('button:has-text("Log in"), a:has-text("Log in"), button:has-text("Войти"), a:has-text("Войти")').count()
        except Exception:
            login_visible = 0
        if login_visible:
            raise NeedsLogin("ChatGPT не принял cookies. Обнови их в админке.")
        raise NeedsLogin(
            "ChatGPT не открыл поле ввода. Cookies могли истечь или сайт запросил дополнительную проверку."
        )

    async def _open_chat(self, force: bool = False) -> None:
        if not force and self.new_chat:
            btn = await self._first(NEW_CHAT_SELECTORS, timeout=1000)
            if btn is not None:
                with contextlib.suppress(Exception):
                    await btn.click()
                    await self._page.wait_for_timeout(600)
                    return
        if force or self.new_chat:
            try:
                await self._page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise TeacherError("Не удалось открыть ChatGPT: %s" % exc)
            await self._page.wait_for_timeout(1600)
        await self._check_login()

    async def _responses(self):
        for sel in RESPONSE_SELECTORS:
            loc = self._page.locator(sel)
            try:
                if await loc.count():
                    return loc
            except Exception:
                continue
        return self._page.locator(RESPONSE_SELECTORS[-1])

    async def _count_responses(self) -> int:
        try:
            return await (await self._responses()).count()
        except Exception:
            return 0

    async def _generating(self) -> bool:
        for sel in STOP_SELECTORS:
            try:
                if await self._page.locator(sel).first.is_visible(timeout=250):
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

    async def _write(self, editor, text: str) -> None:
        tag = (await editor.evaluate("el => el.tagName.toLowerCase()")) if editor else ""
        try:
            if tag == "textarea":
                await editor.fill(text)
            else:
                await editor.click()
                await editor.evaluate("(el, value) => { el.focus(); el.innerText = ''; document.execCommand('insertText', false, value); el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:value})); }", text)
        except Exception:
            await editor.click()
            with contextlib.suppress(Exception):
                await self._page.keyboard.press("Control+A")
            await self._page.keyboard.type(text, delay=1)
        await self._page.wait_for_timeout(250)

    async def _send(self, editor) -> None:
        button = await self._first(SEND_SELECTORS, timeout=2500)
        if button is not None:
            with contextlib.suppress(Exception):
                await button.click()
                return
        await editor.press("Enter")

    async def _attachment_count(self) -> int:
        total = 0
        for sel in PREVIEW_SELECTORS:
            with contextlib.suppress(Exception):
                total += await self._page.locator(sel).count()
        return total

    async def _attach(self, media: Sequence[MediaItem]) -> None:
        paths = [str(x.path) for x in media]
        before = await self._attachment_count()
        inputs = self._page.locator('input#upload-files[type="file"], input[type="file"]')
        try:
            count = await inputs.count()
        except Exception:
            count = 0
        if not count:
            btn = await self._first(ATTACH_BUTTONS, timeout=3500)
            if btn is not None:
                with contextlib.suppress(Exception):
                    await btn.click()
                    await self._page.wait_for_timeout(500)
            try:
                count = await inputs.count()
            except Exception:
                count = 0
        for i in range(min(count, 4)):
            try:
                await inputs.nth(i).set_input_files(paths)
                for _ in range(30):
                    if await self._attachment_count() > before:
                        self._files_sent += len(media)
                        return
                    await self._page.wait_for_timeout(350)
                # Некоторые версии интерфейса не имеют стабильного preview-селектора.
                self._files_sent += len(media)
                return
            except Exception:
                continue
        raise MediaAttachError("ChatGPT не принял вложение через веб-интерфейс")

    async def _wait_answer(self, before: int) -> str:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        appeared = False
        while loop.time() < deadline:
            if await self._count_responses() > before or await self._generating():
                appeared = True
                break
            await self._page.wait_for_timeout(400)
        if not appeared:
            await self._check_login()
            raise TeacherError("ChatGPT не начал отвечать")
        last, stable = "", None
        while loop.time() < deadline:
            text = await self._read_last()
            if text and text == last:
                stable = stable or loop.time()
                if loop.time() - stable >= 2.5 and not await self._generating():
                    return text.strip()
            else:
                last, stable = text, None
            await self._page.wait_for_timeout(500)
        if last.strip():
            return last.strip()
        raise TeacherError("Истекло время ожидания ответа ChatGPT")

    @staticmethod
    def _clean(text: str) -> str:
        text = re.sub(r"^\s*ChatGPT\s*(said|ответил)?\s*[:\-—]?\s*", "", text or "", flags=re.I)
        return re.sub(r"\n{3,}", "\n\n", text).strip()

    async def ask(self, question: str, context: List[Dict[str, str]],
                  media: Optional[Sequence[MediaItem]] = None) -> str:
        message = prompt_mod.build_web_message(self.bot_name, question, context, media)
        async with self._lock:
            await self._ensure()
            await self._open_chat()
            editor = await self._first(EDITOR_SELECTORS, timeout=15000)
            await self._check_login()
            if editor is None:
                raise TeacherError("Не найдено поле ввода ChatGPT")
            before = await self._count_responses()
            if media:
                await self._attach(media)
                editor = await self._first(EDITOR_SELECTORS, timeout=10000) or editor
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
        return True, "чат открыт · вопросов %d · файлов %d" % (self._asked, self._files_sent)
