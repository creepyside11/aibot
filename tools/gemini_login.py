"""Разовая настройка доступа к чату Gemini (режим web).

Скрипт только открывает окно браузера с профилем бота.
Окно про cookie и вход в Google-аккаунт проходит человек — скрипт
ничего не нажимает за тебя, логин и пароль не запрашивает и не хранит.
Сессия остаётся в data/gemini_profile, дальше бот работает сам.

Запуск:  python tools/gemini_login.py
Важно: бот в это время должен быть остановлен — профиль занимает один процесс.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402

STATE_JS = """() => {
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], mat-dialog-container'));
  const consent = dialogs.some(d => /cookie|файлы cookie|Прежде чем перейти|Before you continue/i
      .test(d.innerText || ''));
  const signInLink = Array.from(document.querySelectorAll('a, button')).some(el =>
      /^(войти|sign in)$/i.test((el.innerText || '').trim()));
  const composer = !!document.querySelector(
      'div.ql-editor[contenteditable="true"], div[contenteditable="true"][role="textbox"]');
  return {consent, signInLink, composer};
}"""


async def main() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Сначала выполни: pip install playwright")
        return

    print("Профиль бота:", config.GEMINI_PROFILE_DIR)
    print()
    print("Что нужно сделать в открывшемся окне:")
    print("  1) окно Google про cookie — выбери вариант сам")
    print("     (для приватности достаточно «Отклонить все»);")
    print("  2) нажми «Войти» и зайди в свой Google-аккаунт;")
    print("  3) дождись, пока появится поле «Спросить Gemini».")
    print()
    print("Затем вернись сюда и нажми Enter.\n")

    pw = await async_playwright().start()
    kwargs = dict(
        user_data_dir=str(config.GEMINI_PROFILE_DIR),
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="ru-RU",
        args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
    )
    try:
        ctx = await pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception:
        ctx = await pw.chromium.launch_persistent_context(**kwargs)

    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, input, "Нажми Enter, когда закончишь... ")

    try:
        await page.goto(config.GEMINI_WEB_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        state = await page.evaluate(STATE_JS)
    except Exception as exc:
        state = {"error": str(exc)}

    await ctx.close()
    await pw.stop()

    print()
    if state.get("consent"):
        print("⚠️  Окно про cookie всё ещё висит — запусти скрипт ещё раз и закрой его.")
    elif state.get("signInLink") and not state.get("composer"):
        print("⚠️  Вход в аккаунт не завершён — запусти скрипт ещё раз.")
    elif state.get("composer"):
        print("✅ Готово: чат Gemini открывается, поле ввода на месте.")
        print("   Теперь можно запускать бота:  python main.py")
    else:
        print("⚠️  Не удалось подтвердить доступ:", state)
        print("   Попробуй ещё раз, дождавшись загрузки чата.")


if __name__ == "__main__":
    asyncio.run(main())
