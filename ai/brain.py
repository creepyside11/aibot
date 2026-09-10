"""Мозг: сначала вспоминает сам, и только если не знает — спрашивает учителя."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Sequence

from .media import MediaItem, default_question, describe
from .providers.base import (MediaAttachError, MediaUnsupported, NeedsLogin,
                            Teacher, TeacherError)
from .storage import Storage
from .text import is_context_dependent, looks_reusable

log = logging.getLogger(__name__)


class Answer:
    __slots__ = ("text", "source", "score", "latency_ms", "learned_id", "ok")

    def __init__(self, text: str, source: str, score: float = 0.0,
                 latency_ms: int = 0, learned_id: int = 0, ok: bool = True):
        self.text = text
        self.source = source          # brain | teacher | error
        self.score = score            # насколько похож вспомненный вопрос
        self.latency_ms = latency_ms
        self.learned_id = learned_id
        self.ok = ok

    @property
    def from_memory(self) -> bool:
        return self.source == "brain"


class Brain:
    def __init__(self, storage: Storage, teacher: Teacher, context_turns: int = 12):
        self.storage = storage
        self.teacher = teacher
        self.context_turns = context_turns
        self.on_alert = None  # корутина: сообщить админу о поломке
        self._locks: Dict[int, asyncio.Lock] = {}

    def lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._locks:
            self._locks[user_id] = asyncio.Lock()
        return self._locks[user_id]

    def busy(self, user_id: int) -> bool:
        lock = self._locks.get(user_id)
        return bool(lock and lock.locked())

    # ---------------- основной цикл мышления ----------------
    async def respond(self, user_id: int, question: str,
                      media: Optional[Sequence[MediaItem]] = None) -> Answer:
        started = time.time()
        question = (question or "").strip()
        media = list(media or [])
        if not question and not media:
            return Answer("Напиши что-нибудь — я отвечу.", "brain")
        if not question:
            # файл без подписи: формулируем просьбу за пользователя
            question = default_question(media)

        context = self.storage.get_context(user_id, self.context_turns)
        # файл каждый раз новый: память тут не поможет, поэтому сразу к учителю
        dependent = is_context_dependent(question) or bool(media)
        tag = describe(media)

        # 1. пробуем вспомнить сами
        if self.storage.flag("brain_first") and not dependent:
            recalled = self.storage.recall(question)
            if recalled is not None:
                row, score = recalled
                if score >= self.storage.threshold():
                    self.storage.register_hit(row["id"])
                    answer = Answer(row["answer"], "brain", score,
                                    int((time.time() - started) * 1000))
                    self._remember_turn(user_id, question, answer.text)
                    self.storage.add_message(user_id, question, answer.text,
                                             "brain", answer.latency_ms)
                    return answer

        # 2. не знаем — идём в чат к учителю
        try:
            text = await self.teacher.ask(question, context, media)
        except MediaAttachError as exc:
            self.storage.log("error", "attach", str(exc))
            self._alert("Файл не прикрепляется в чат Gemini: %s" % exc)
            return Answer(
                "😕 Не смог донести файл до своего источника знаний. "
                "Попробуй прислать ещё раз — или опиши словами, что на нём.",
                "error", ok=False,
                latency_ms=int((time.time() - started) * 1000),
            )
        except MediaUnsupported as exc:
            self.storage.log("warn", "media", str(exc))
            return Answer(
                "😕 Такой файл я прочитать не смогу: %s" % exc,
                "error", ok=False,
                latency_ms=int((time.time() - started) * 1000),
            )
        except NeedsLogin as exc:
            self.storage.log("error", "login", str(exc))
            self._alert("Учитель недоступен: %s" % exc)
            return Answer(
                "⚠️ Я потерял доступ к своему источнику знаний.\n"
                "Админ уже уведомлён — попробуй чуть позже.",
                "error", ok=False,
                latency_ms=int((time.time() - started) * 1000),
            )
        except TeacherError as exc:
            self.storage.log("error", "teacher", str(exc))
            self._alert("Сбой учителя: %s" % exc)
            fallback = self._closest_known(question)
            if fallback is not None:
                row, score = fallback
                self.storage.register_hit(row["id"])
                answer = Answer(row["answer"], "brain", score,
                                int((time.time() - started) * 1000))
                self._remember_turn(user_id, question, answer.text)
                self.storage.add_message(user_id, question, answer.text, "brain",
                                         answer.latency_ms)
                return answer
            return Answer(
                "⚠️ Не смог получить ответ прямо сейчас. Напиши ещё раз через минуту.",
                "error", ok=False,
                latency_ms=int((time.time() - started) * 1000),
            )

        latency = int((time.time() - started) * 1000)
        text = (text or "").strip() or "Мне нечего добавить."

        # 3. запоминаем — это и есть обучение
        learned_id = 0
        if not media and self.storage.flag("learning") and looks_reusable(question, text):
            try:
                learned_id = self.storage.learn(question, text, self.teacher.name, user_id)
            except Exception as exc:
                log.warning("Не удалось запомнить ответ: %s", exc)

        answer = Answer(text, "teacher", 0.0, latency, learned_id)
        self._remember_turn(user_id, self._tagged(question, tag), text)
        self.storage.add_message(user_id, self._tagged(question, tag), text,
                                 "teacher", latency,
                                 media=media[0].kind if len(media) == 1
                                 else ("mixed" if media else ""))
        return answer

    @staticmethod
    def _tagged(question: str, tag: str) -> str:
        """Помечает реплику видом вложения, чтобы контекст оставался понятным."""
        return ("%s %s" % (tag, question)).strip() if tag else question

    def _alert(self, text: str) -> None:
        if self.on_alert:
            asyncio.create_task(self.on_alert(text))

    def _remember_turn(self, user_id: int, question: str, answer: str) -> None:
        self.storage.add_context(user_id, "user", question, self.context_turns)
        self.storage.add_context(user_id, "assistant", answer, self.context_turns)

    def _closest_known(self, question: str, floor: float = 0.62):
        """Если учитель недоступен — отвечаем лучшим, что уже знаем."""
        recalled = self.storage.recall(question)
        if recalled and recalled[1] >= floor:
            return recalled
        return None

    # ---------------- обучение без пользователя ----------------
    async def study(self, question: str) -> Optional[int]:
        """Задаёт учителю вопрос по своей инициативе и запоминает ответ."""
        known = self.storage.recall(question)
        if known is not None and known[1] >= 0.95:
            return None  # уже знаем
        try:
            text = await self.teacher.ask(question, [])
        except (TeacherError, NeedsLogin) as exc:
            self.storage.log("warn", "autolearn", str(exc))
            return None
        if not looks_reusable(question, text):
            return None
        kb_id = self.storage.learn(question, text.strip(), self.teacher.name, 0)
        self.storage.log("info", "autolearn", "выучен вопрос: %s" % question[:120])
        return kb_id
