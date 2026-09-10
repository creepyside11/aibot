"""Работа с текстом: нормализация и оценка схожести вопросов.

Всё на чистом Python — без numpy/sklearn, чтобы мозг запускался где угодно.
"""
from __future__ import annotations

import re
from collections import Counter
from math import sqrt

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

# слова-отсылки к предыдущей реплике: такие вопросы нельзя брать из памяти,
# их смысл зависит от контекста диалога
_ANAPHORA = {
    "он", "она", "оно", "они", "его", "ее", "её", "их", "им", "ему", "ей",
    "это", "этот", "эта", "эти", "то", "том", "тот", "та", "те", "там",
    "тогда", "туда", "так", "такое", "такой", "еще", "ещё", "дальше",
    "подробнее", "продолжи", "продолжай", "переведи", "почему", "зачем",
    "а", "и", "ну", "ок", "давай", "объясни", "поясни", "уточни", "напиши",
    "it", "they", "this", "that", "these", "those", "more", "continue",
    "why", "how", "explain", "translate", "go", "on",
}

_FOLLOWUP_START = (
    "а ", "и ", "ну ", "тогда", "еще ", "ещё ", "продолж", "дальше",
    "подробнее", "переведи", "объясни это", "а что", "а как", "а почему",
    "and ", "also ", "what about", "continue", "go on",
)


def normalize(text: str) -> str:
    """Приводит фразу к каноническому виду для сравнения и поиска."""
    t = (text or "").lower().replace("ё", "е").replace(" ", " ")
    t = _PUNCT.sub(" ", t)
    return _SPACES.sub(" ", t).strip()


def words(norm_text: str) -> set:
    return set(w for w in norm_text.split() if w)


def ngram_vector(norm_text: str, n: int = 3) -> Counter:
    """Вектор символьных n-грамм — устойчив к опечаткам и окончаниям."""
    t = " " + norm_text + " "
    if len(t) <= n:
        return Counter([t])
    return Counter(t[i:i + n] for i in range(len(t) - n + 1))


def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    dot = 0
    for key, val in small.items():
        other = big.get(key)
        if other:
            dot += val * other
    if not dot:
        return 0.0
    na = sqrt(sum(v * v for v in a.values()))
    nb = sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / float(len(a | b))


def similarity(norm_a: str, norm_b: str) -> float:
    """Итоговая близость двух нормализованных фраз, 0..1."""
    if norm_a == norm_b:
        return 1.0
    if not norm_a or not norm_b:
        return 0.0
    ratio = 0.65 * cosine(ngram_vector(norm_a), ngram_vector(norm_b))
    ratio += 0.35 * jaccard(words(norm_a), words(norm_b))
    # сильная разница в длине почти всегда означает разные вопросы
    la, lb = len(norm_a), len(norm_b)
    penalty = min(la, lb) / float(max(la, lb))
    return ratio * (0.5 + 0.5 * penalty)


def is_context_dependent(text: str) -> bool:
    """True, если фраза не самостоятельна и её нельзя выучить/достать из памяти."""
    norm = normalize(text)
    parts = norm.split()
    if not parts:
        return True
    if norm.startswith(_FOLLOWUP_START):
        return True
    if len(parts) <= 5 and (set(parts) & _ANAPHORA):
        return True
    return False


def looks_reusable(question: str, answer: str) -> bool:
    """Стоит ли вообще запоминать эту пару вопрос-ответ."""
    if not question or not answer:
        return False
    norm = normalize(question)
    if len(norm) < 2 or len(norm) > 400:
        return False
    if len(answer.strip()) < 2:
        return False
    return not is_context_dependent(question)
