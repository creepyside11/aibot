"""Самообучение: в свободное время ИИ сам задаёт учителю новые вопросы."""
from __future__ import annotations

import asyncio
import logging
import random

from .brain import Brain
from .prompt import SELF_STUDY_PROMPT
from .providers.base import NeedsLogin, TeacherError

log = logging.getLogger(__name__)

# темы «любопытства» на старте, пока своя база знаний пустая
SEED_TOPICS = [
    "повседневное общение и приветствия", "интересные факты о космосе",
    "здоровье и привычки", "история изобретений", "языки программирования",
    "психология общения", "кулинария", "финансовая грамотность",
    "путешествия", "искусственный интеллект", "спорт и тренировки",
    "музыка и кино", "природа и животные", "наука простыми словами",
]


class AutoLearner:
    def __init__(self, brain: Brain, interval_sec: int = 900, per_round: int = 3):
        self.brain = brain
        self.interval = interval_sec
        self.per_round = per_round
        self._task = None
        self.rounds = 0
        self.learned = 0

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    async def _loop(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                if self.brain.storage.flag("autolearn", False):
                    await self._round()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Сбой самообучения: %s", exc)
            await asyncio.sleep(self.interval)

    async def _round(self) -> None:
        seeds = self.brain.storage.learn_seeds(5)
        topic = random.choice(seeds) if seeds and random.random() < 0.6 \
            else random.choice(SEED_TOPICS)
        try:
            raw = await self.brain.teacher.ask(
                SELF_STUDY_PROMPT.format(n=self.per_round, topic=topic), []
            )
        except (TeacherError, NeedsLogin) as exc:
            log.info("Самообучение отложено: %s", exc)
            return

        questions = []
        for line in raw.split("\n"):
            line = line.strip(" -*0123456789.•\t")
            if 5 <= len(line) <= 200 and "?" in line:
                questions.append(line)
        self.rounds += 1

        for question in questions[: self.per_round]:
            try:
                if await self.brain.study(question):
                    self.learned += 1
            except Exception as exc:
                log.debug("Вопрос пропущен: %s", exc)
            await asyncio.sleep(5)
