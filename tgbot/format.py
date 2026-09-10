"""Превращение markdown-ответов Gemini в аккуратный HTML для Telegram."""
from __future__ import annotations

import html
import re
from typing import List

MAX_LEN = 3400


def split_source(text: str, limit: int = MAX_LEN) -> List[str]:
    """Режем ещё до разметки, чтобы не порвать HTML-теги пополам."""
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks, current = [], ""
    for block in text.split("\n"):
        if len(current) + len(block) + 1 > limit:
            if current:
                chunks.append(current)
                current = ""
            while len(block) > limit:
                chunks.append(block[:limit])
                block = block[limit:]
        current = block if not current else current + "\n" + block
    if current:
        chunks.append(current)
    return chunks


def md_to_html(md: str) -> str:
    stash: List[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return "\x00%d\x00" % (len(stash) - 1)

    def code_block(match) -> str:
        lang = (match.group(1) or "").strip()
        body = html.escape(match.group(2))
        if lang:
            return keep('<pre><code class="language-%s">%s</code></pre>'
                        % (html.escape(lang), body))
        return keep("<pre>%s</pre>" % body)

    text = re.sub(r"```(\w*)\n(.*?)```", code_block, md or "", flags=re.S)
    text = re.sub(r"`([^`\n]+)`",
                  lambda m: keep("<code>%s</code>" % html.escape(m.group(1))), text)

    text = html.escape(text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*(.+?)\s*$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"^\s*([\*\-\+])\s+", "• ", text, flags=re.M)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text, flags=re.S)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w\*])\*(?!\s)(.+?)(?<!\s)\*(?![\w\*])", r"<i>\1</i>", text, flags=re.S)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"\[(.+?)\]\((https?://[^\s)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    for index, fragment in enumerate(stash):
        text = text.replace("\x00%d\x00" % index, fragment)
    return text.strip()


def to_telegram(text: str) -> List[str]:
    return [md_to_html(chunk) for chunk in split_source(text) if chunk.strip()]


def user_title(row) -> str:
    """Читаемое имя пользователя для админки."""
    name = " ".join(p for p in [row["first_name"], row["last_name"]] if p) or "без имени"
    handle = "@%s" % row["username"] if row["username"] else str(row["user_id"])
    return "%s (%s)" % (html.escape(name), html.escape(handle))


def human_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return "%dд %dч %dм" % (days, hours, minutes)
    if hours:
        return "%dч %dм" % (hours, minutes)
    return "%dм" % minutes


def bar(value: int, top: int, width: int = 12) -> str:
    if top <= 0:
        return "▁" * 0
    filled = int(round(width * value / float(top)))
    return "█" * max(1 if value else 0, filled)
