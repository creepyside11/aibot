"""Учитель через настоящий чат gemini.google.com.

Мой ИИ сам открывает composer («Спросить Gemini»), при необходимости
прикрепляет файлы кнопкой «Добавить файлы», печатает сообщение,
дожидается полного ответа и забирает его. Никакого поиска в Google,
никакой Википедии — единственный источник знаний это ответы Gemini в чате.

Вход в аккаунт Google выполняет только человек: python tools/gemini_login.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import re
from typing import Dict, List, Optional, Sequence, Tuple

from .. import prompt as prompt_mod
from ..media import MediaItem
from .base import MediaAttachError, NeedsLogin, Teacher, TeacherError

log = logging.getLogger(__name__)

# у Gemini регулярно меняется вёрстка — держим набор запасных селекторов
EDITOR_SELECTORS = (
    'rich-textarea div.ql-editor[contenteditable="true"]',
    'div.ql-editor[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
    'textarea[aria-label]',
)
SEND_SELECTORS = (
    'button.send-button:not([disabled])',
    'button[aria-label*="Отправить"]:not([disabled])',
    'button[aria-label*="Send"]:not([disabled])',
    'button[mattooltip*="Отправить"]:not([disabled])',
)
# сначала — внутренний блок с текстом, потом контейнер целиком:
# так в ответ не попадают подписи интерфейса вроде «Ответ Gemini»
RESPONSE_SELECTORS = (
    'message-content.model-response-text',
    '.model-response-text .markdown',
    '.model-response-text',
    'model-response .markdown',
    'model-response',
    'response-container',
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
    'expanded-button[aria-label*="Новый чат"]',
)
# кнопка «+» у поля ввода. У залогиненного интерфейса она называется
# «Загрузка и инструменты», у гостевого — «Добавить файлы»
ATTACH_SELECTORS = (
    'button[aria-label="Загрузка и инструменты"]',
    'button[aria-label*="Загрузка и инструмент"]',
    'button[aria-label*="Upload and tools"]',
    'button[aria-label="Добавить файлы"]',
    'button[aria-label*="Добавить файл"]',
    'button[aria-label*="Add files"]',
    'button[aria-label*="Прикрепить"]',
    'button[aria-label*="Attach"]',
    'toolbox-drawer button',
)
# скрытый input появляется в DOM только после открытия этого меню
HIDDEN_INPUT = 'input.hidden-file-input'
UPLOAD_MENU_SELECTORS = (
    'button[aria-label^="Загрузить файлы"]',
    'button[aria-label^="Upload files"]',
    '[role="menuitem"]:has-text("Загрузить файлы")',
    'button:has-text("Загрузить файлы")',
)
# превью прикреплённого файла — по ним понятно, что вложение реально дошло
PREVIEW_SELECTORS = (
    'uploader-file-preview',
    '.file-preview-chip',
    'uploader-file-preview-container',
    'gem-media-attachment',
    '.attachment-preview-wrapper',
    'img.gem-attachment-style-img',
)
UPLOAD_BUSY_SELECTORS = (
    '.gem-attachment-loading-container',
    '[role="progressbar"]',
    'mat-progress-bar',
    'mat-spinner',
)
LOGIN_MARKERS = ("accounts.google.com", "ServiceLogin", "signin")

# служебные подписи интерфейса, которые не должны попадать в ответ бота
JUNK_LINES = {
    "ответ gemini", "gemini", "ответ", "показать детали", "показать ход мыслей",
    "показать процесс рассуждения", "показать черновики", "черновики",
    "show thinking", "show details", "show drafts", "gemini's response",
    "поделиться", "скопировать", "ещё", "хороший ответ", "плохой ответ",
    "копировать", "copy", "share", "google аккаунт",
}
JUNK_PREFIX = re.compile(r"^\s*(ответ\s+gemini|gemini'?s?\s+response|gemini|ответ)\s*[:\-—]\s*",
                         re.IGNORECASE)


class GeminiWebTeacher(Teacher):
    name = "web"
    title = "Чат gemini.google.com"

    def __init__(self, url: str, profile_dir, bot_name: str,
                 headless: bool = False, timeout: int = 180, new_chat: bool = True):
        self.url = url
        self.profile_dir = str(profile_dir)
        self.bot_name = bot_name
        self.headless = headless
        self.timeout = timeout
        self.new_chat = new_chat
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self._asked = 0
        self._files_sent = 0
        self._last_way = ""

    # ---------------- жизненный цикл браузера ----------------
    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise TeacherError(
                "Не установлен Playwright. Выполни: pip install playwright && "
                "python -m playwright install chromium"
            ) from exc

        self._pw = await async_playwright().start()
        launch_kwargs = dict(
            user_data_dir=self.profile_dir,
            headless=self.headless,
            viewport={"width": 1280, "height": 900},
            locale="ru-RU",
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                channel="chrome", **launch_kwargs
            )
        except Exception:  # системного Chrome нет — берём встроенный Chromium
            self._ctx = await self._pw.chromium.launch_persistent_context(**launch_kwargs)

        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        self._page.set_default_timeout(30000)
        await self._open_chat(force=True)
        log.info("Браузер с Gemini запущен, профиль: %s", self.profile_dir)

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

    # ---------------- состояние страницы ----------------
    async def _first(self, selectors, timeout: int = 1500):
        for sel in selectors:
            try:
                loc = self._page.locator(sel).first
                await loc.wait_for(state="visible", timeout=timeout)
                return loc
            except Exception:
                continue
        return None

    async def _page_state(self) -> dict:
        """Что сейчас на странице: окно про cookie, гость или рабочий чат."""
        try:
            return await self._page.evaluate(
                """() => {
                  const dialogs = Array.from(
                      document.querySelectorAll('[role="dialog"], mat-dialog-container'));
                  const consent = dialogs.some(d => /cookie|файлы cookie|Прежде чем перейти|Before you continue/i
                      .test(d.innerText || ''));
                  const signInLink = Array.from(document.querySelectorAll('a, button')).some(el =>
                      /^(войти|sign in)$/i.test((el.innerText || '').trim()));
                  const composer = !!document.querySelector(
                      'div.ql-editor[contenteditable="true"], div[contenteditable="true"][role="textbox"]');
                  return {consent, signInLink, composer};
                }"""
            )
        except Exception:
            return {"consent": False, "signInLink": False, "composer": False}

    async def _check_login(self) -> None:
        """Решения про cookie и вход принимает человек, бот их не кликает."""
        url = (self._page.url or "").lower()
        if any(marker.lower() in url for marker in LOGIN_MARKERS):
            raise NeedsLogin(
                "Нужен вход в аккаунт Google. Останови бота и выполни: "
                "python tools/gemini_login.py"
            )
        state = await self._page_state()
        if state.get("consent"):
            raise NeedsLogin(
                "Google показывает окно про cookie и перекрывает чат. "
                "Выполни: python tools/gemini_login.py — там выбери вариант вручную."
            )
        if state.get("signInLink") and not state.get("composer"):
            raise NeedsLogin(
                "Аккаунт Google не подключён. Выполни: python tools/gemini_login.py"
            )

    async def _open_chat(self, force: bool = False) -> None:
        """Открывает чистый чат: контекст держит мой ИИ, а не Gemini."""
        if force or self.new_chat:
            button = await self._first(NEW_CHAT_SELECTORS, timeout=1200) if not force else None
            if button is not None:
                try:
                    await button.click()
                    await self._page.wait_for_timeout(700)
                    return
                except Exception:
                    pass
            try:
                await self._page.goto(self.url, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise TeacherError("Не удалось открыть Gemini: %s" % exc)
            await self._page.wait_for_timeout(1500)
        await self._check_login()

    # ---------------- прикрепление файлов ----------------
    async def _file_inputs(self) -> int:
        try:
            return await self._page.locator('input[type="file"]').count()
        except Exception:
            return 0

    async def _attachment_count(self) -> int:
        """Сколько вложений сейчас видно в композере.

        Считаем обобщённо, без опоры на имена классов Gemini: превью картинок
        и любые карточки-чипы файлов. Важен не сам счёт, а его прирост.
        """
        try:
            return int(await self._page.evaluate(
                """() => {
                  let n = document.querySelectorAll([
                      'uploader-file-preview', '.file-preview-chip',
                      'uploader-file-preview-container', 'gem-media-attachment',
                      '.attachment-preview-wrapper', 'img.gem-attachment-style-img'
                  ].join(',')).length;
                  n += document.querySelectorAll(
                      'img[src^="blob:"], img[src^="data:image"]').length;
                  n += document.querySelectorAll([
                      '[data-test-id*="file"]', '[class*="file-preview"]',
                      '[class*="attachment"]', '[class*="uploaded"]'
                  ].join(',')).length;
                  return n;
                }"""
            ))
        except Exception:
            return 0

    async def _wait_attached(self, before: int, limit: float = 12.0) -> bool:
        """Ждём, пока вложение реально появится в композере."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + limit
        while loop.time() < deadline:
            if await self._attachment_count() > before:
                return True
            if await self._upload_busy():  # идёт загрузка — значит, файл принят
                return True
            await self._page.wait_for_timeout(400)
        return False

    async def _attach_via_input(self, paths: Sequence[str], nth: int = 0) -> bool:
        """Самый надёжный путь: положить файлы прямо в скрытый input.

        Загрузчиков на странице бывает несколько (картинки, файлы, код),
        поэтому пробуем их по очереди, а не только первый попавшийся.
        """
        if await self._file_inputs() <= nth:
            return False
        try:
            await self._page.locator('input[type="file"]').nth(nth).set_input_files(list(paths))
            return True
        except Exception as exc:
            log.debug("input[type=file] #%d не принял файлы: %s", nth, exc)
            return False

    async def _open_attach_menu(self) -> bool:
        """Жмёт «+» у поля ввода — только после этого в DOM появляется input."""
        button = await self._first(ATTACH_SELECTORS, timeout=5000)
        if button is None:
            return False
        try:
            await button.evaluate("(el) => el.click()")
        except Exception:
            try:
                await button.click(timeout=5000)
            except Exception:
                return False
        try:
            await self._page.locator(HIDDEN_INPUT).first.wait_for(
                state="attached", timeout=6000)
            return True
        except Exception:
            return False

    async def _attach_via_menu(self, paths: Sequence[str]) -> bool:
        """Основной путь: «Загрузка и инструменты» → скрытый input → файлы."""
        if not await self._open_attach_menu():
            return False
        try:
            await self._page.locator(HIDDEN_INPUT).first.set_input_files(list(paths))
        except Exception as exc:
            log.debug("Скрытый input меню не принял файлы: %s", exc)
            with contextlib.suppress(Exception):
                await self._page.keyboard.press("Escape")
            return False
        # закрываем меню, чтобы оно не перекрывало поле ввода и кнопку отправки
        with contextlib.suppress(Exception):
            await self._page.keyboard.press("Escape")
        await self._page.wait_for_timeout(400)
        return True

    async def _click_upload_menu_item(self) -> bool:
        """В меню кнопки «+» выбирает загрузку с компьютера, а не с Диска."""
        try:
            return bool(await self._page.evaluate(
                """() => {
                  const items = Array.from(document.querySelectorAll(
                      '[role="menuitem"], mat-menu-item, .mat-mdc-menu-item, [role="menu"] button'));
                  const good = items.find(el => {
                    const t = (el.innerText || '').toLowerCase();
                    return /загруз|upload|компьютер|файл|files/.test(t)
                        && !/диск|drive|код|code|фото google|google photos/.test(t);
                  });
                  if (!good) return false;
                  good.click();
                  return true;
                }"""
            ))
        except Exception:
            return False

    async def _attach_via_chooser(self, paths: Sequence[str]) -> bool:
        """Через кнопку «Добавить файлы» и системный диалог выбора."""
        try:
            async with self._page.expect_file_chooser(timeout=15000) as info:
                button = await self._first(ATTACH_SELECTORS, timeout=4000)
                if button is None:
                    raise TeacherError("нет кнопки «Добавить файлы»")
                try:
                    await button.evaluate("(el) => el.click()")
                except Exception:
                    await button.click(timeout=5000)
                # у кнопки может быть меню — тогда жмём пункт про загрузку файлов
                await self._page.wait_for_timeout(900)
                await self._click_upload_menu_item()
            chooser = await info.value
            await chooser.set_files(list(paths))
            return True
        except Exception as exc:
            log.debug("Диалог выбора файлов не сработал: %s", exc)
            with contextlib.suppress(Exception):
                await self._page.keyboard.press("Escape")
            return False

    async def _attach_via_drop(self, media: Sequence[MediaItem]) -> bool:
        """Запасной путь: имитируем перетаскивание файла в окно чата."""
        payload = []
        for item in media:
            try:
                raw = item.path.read_bytes()
            except OSError:
                continue
            if len(raw) > 15 * 1024 * 1024:  # через страницу такое тащить нельзя
                return False
            payload.append({
                "name": item.name,
                "mime": item.mime,
                "b64": base64.b64encode(raw).decode("ascii"),
            })
        if not payload:
            return False
        try:
            return bool(await self._page.evaluate(
                """(files) => {
                  const dt = new DataTransfer();
                  for (const f of files) {
                    const bin = atob(f.b64);
                    const arr = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                    dt.items.add(new File([arr], f.name, {type: f.mime}));
                  }
                  // слушатель может висеть на чём угодно — раздаём событие всем
                  const targets = [
                    document.querySelector('div.ql-editor[contenteditable="true"]'),
                    document.querySelector('rich-textarea'),
                    document.querySelector('input-container'),
                    document.querySelector('file-drop-indicator'),
                    document.body, document.documentElement, window,
                  ].filter(Boolean);
                  let fired = 0;
                  for (const target of targets) {
                    for (const type of ['dragenter', 'dragover', 'drop']) {
                      const ev = new DragEvent(type, {
                        bubbles: true, cancelable: true, composed: true, dataTransfer: dt});
                      target.dispatchEvent(ev);
                    }
                    fired++;
                  }
                  return fired > 0;
                }""",
                payload,
            ))
        except Exception as exc:
            log.debug("Перетаскивание не сработало: %s", exc)
            return False

    async def _attach(self, media: Sequence[MediaItem]) -> None:
        """Перебирает способы, пока вложение не окажется в композере на самом деле."""
        paths = [str(item.path) for item in media]
        weight = sum(item.size for item in media)
        before = await self._attachment_count()

        ways = [("меню «Загрузка и инструменты»", lambda: self._attach_via_menu(paths))]
        for i in range(min(await self._file_inputs(), 3)):
            ways.append(("скрытый input #%d" % i,
                         lambda i=i: self._attach_via_input(paths, i)))
        ways.append(("диалог выбора файлов", lambda: self._attach_via_chooser(paths)))
        # диалог мог создать новый input — проходим по ним ещё раз
        ways.append(("input после диалога", lambda: self._attach_via_input(paths, 0)))
        ways.append(("перетаскивание", lambda: self._attach_via_drop(media)))
        for label, action in ways:
            try:
                accepted = await action()
            except Exception as exc:
                log.debug("Способ «%s» упал: %s", label, exc)
                continue
            if not accepted:
                continue
            if await self._wait_attached(before):
                log.info("Файл прикреплён способом: %s", label)
                self._last_way = label
                await self._wait_upload(weight)
                return
            log.warning("Способ «%s» не дал вложения, пробую следующий", label)

        raise MediaAttachError(
            "ни один способ не сработал (пробовал: input, диалог выбора, перетаскивание)"
        )

    async def _upload_busy(self) -> bool:
        for sel in UPLOAD_BUSY_SELECTORS:
            try:
                if await self._page.locator(sel).first.is_visible(timeout=200):
                    return True
            except Exception:
                continue
        return False

    async def _wait_upload(self, total_bytes: int = 0, limit: int = 180) -> None:
        """Ждём, пока Gemini дожуёт загрузку: иначе отправка уйдёт без файла."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + limit
        # чем тяжелее файл, тем дольше минимальная пауза: 1.5 с + 0.6 с на мегабайт
        floor = loop.time() + min(45.0, 1.5 + 0.6 * (total_bytes / (1024.0 * 1024.0)))
        await self._page.wait_for_timeout(1000)
        quiet = 0
        while loop.time() < deadline:
            busy = await self._upload_busy()
            if busy:
                quiet = 0
            else:
                quiet += 1
            # готово: индикатор пропал, минимальная пауза вышла, отправка доступна
            if quiet >= 3 and loop.time() >= floor:
                if await self._first(SEND_SELECTORS, timeout=800) is not None:
                    return
                if quiet >= 8:  # кнопка не проснулась — всё равно пробуем
                    return
            await self._page.wait_for_timeout(500)
        log.warning("Загрузка файла в Gemini затянулась, отправляю как есть")

    # ---------------- ввод и отправка ----------------
    async def _write(self, editor, text: str) -> None:
        """Печатает текст в composer так, как это делает человек."""
        try:
            await editor.evaluate("(el) => el.focus()")
        except Exception:
            with contextlib.suppress(Exception):
                await editor.click(timeout=5000)
        await self._page.wait_for_timeout(120)
        try:
            await editor.evaluate(
                "(el, value) => {"
                "  el.focus();"
                "  document.execCommand('selectAll', false, null);"
                "  document.execCommand('insertText', false, value);"
                "}",
                text,
            )
        except Exception:
            pass

        written = ""
        try:
            written = (await editor.inner_text()) or ""
        except Exception:
            pass
        if len(written.strip()) < min(20, len(text)):
            # запасной путь: посимвольный ввод, переносы строк через Shift+Enter
            try:
                await editor.press("Control+A")
                await editor.press("Backspace")
            except Exception:
                pass
            for i, line in enumerate(text.split("\n")):
                if i:
                    await self._page.keyboard.press("Shift+Enter")
                await self._page.keyboard.type(line, delay=1)
        await self._page.wait_for_timeout(250)

    async def _send(self, editor) -> None:
        button = await self._first(SEND_SELECTORS, timeout=2500)
        if button is not None:
            try:
                await button.click()
                return
            except Exception:
                pass
        await editor.press("Enter")

    # ---------------- чтение ответа ----------------
    async def _responses(self):
        for sel in RESPONSE_SELECTORS:
            loc = self._page.locator(sel)
            try:
                if await loc.count():
                    return loc
            except Exception:
                continue
        return self._page.locator(RESPONSE_SELECTORS[-2])

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
                continue
        return False

    async def _read_last(self) -> str:
        try:
            loc = await self._responses()
            count = await loc.count()
            if not count:
                return ""
            return (await loc.nth(count - 1).inner_text()) or ""
        except Exception:
            return ""

    async def _wait_answer(self, before: int) -> str:
        """Ждёт, пока Gemini допишет ответ, и забирает его текст."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.timeout
        appeared = False
        while loop.time() < deadline:
            if await self._count_responses() > before:
                appeared = True
                break
            if await self._generating():
                appeared = True
                break
            await self._page.wait_for_timeout(400)
        if not appeared:
            await self._check_login()
            raise TeacherError("Gemini не начал отвечать — возможно, изменилась вёрстка чата")

        last_text, stable_since = "", None
        while loop.time() < deadline:
            text = await self._read_last()
            if text and text == last_text:
                if stable_since is None:
                    stable_since = loop.time()
                # ответ не меняется 2.5 с и кнопка «Остановить» пропала
                elif loop.time() - stable_since >= 2.5:
                    if not await self._generating():
                        return text.strip()
            else:
                last_text, stable_since = text, None
            await self._page.wait_for_timeout(500)

        if last_text.strip():
            return last_text.strip()
        raise TeacherError("Истекло время ожидания ответа Gemini")

    @staticmethod
    def _clean(text: str) -> str:
        """Убирает подписи интерфейса: «Ответ Gemini», «Показать детали» и т.п."""
        lines = []
        for line in (text or "").split("\n"):
            if line.strip().lower().strip(" .:—-") in JUNK_LINES:
                continue
            lines.append(line)
        result = "\n".join(lines).strip()
        result = JUNK_PREFIX.sub("", result, count=1)
        return re.sub(r"\n{3,}", "\n\n", result).strip()

    # ---------------- основной метод ----------------
    async def ask(self, question: str, context: List[Dict[str, str]],
                  media: Optional[Sequence[MediaItem]] = None) -> str:
        message = prompt_mod.build_web_message(self.bot_name, question, context, media)
        async with self._lock:  # браузер один — запросы идут по очереди
            await self._ensure()
            try:
                await self._open_chat()
                editor = await self._first(EDITOR_SELECTORS, timeout=15000)
                await self._check_login()
                if editor is None:
                    raise TeacherError("Не найдено поле ввода Gemini («Спросить Gemini»)")

                before = await self._count_responses()
                if media:
                    await self._attach(media)
                    self._files_sent += len(media)
                    editor = await self._first(EDITOR_SELECTORS, timeout=10000) or editor
                await self._write(editor, message)
                await self._send(editor)
                answer = await self._wait_answer(before)
                self._asked += 1
                return self._clean(answer)
            except NeedsLogin:
                raise
            except TeacherError:
                raise
            except Exception as exc:
                raise TeacherError("Сбой в чате Gemini: %s" % exc)

    async def health(self) -> Tuple[bool, str]:
        if not await self._alive():
            return False, "браузер не запущен"
        url = self._page.url or ""
        if any(m.lower() in url.lower() for m in LOGIN_MARKERS):
            return False, "требуется вход в Google"
        tail = " · загрузка: %s" % self._last_way if self._last_way else ""
        return True, "чат открыт · вопросов %d · файлов %d%s" % (
            self._asked, self._files_sent, tail)
