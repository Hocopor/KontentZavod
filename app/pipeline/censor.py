"""Слой цензуры контента (этап 8.3): пост-проверка сгенерированного на запреты
рынка РФ/18+ отдельным LLM-вызовом. Fail-open при техническом сбое проверки."""
import json
import logging
import re
from pathlib import Path

from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt

logger = logging.getLogger(__name__)
_FORBIDDEN_PATH = Path(__file__).parent.parent / "prompts" / "agents" / "forbidden_ru.md"


class CensorError(Exception):
    """Контент отклонён цензором после исчерпания попыток регенерации."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _load_forbidden() -> str:
    try:
        return _FORBIDDEN_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def check_content(content_text: str, context: str = "") -> tuple[bool, str]:
    """Проверить текст цензором. Возвращает (ok, reason).

    Пустой текст → (True, ""). Технический сбой проверки (LLMError/невалидный
    JSON) → (True, "") + warning (fail-open: не блокируем из-за сбоя).
    """
    if not content_text or not content_text.strip():
        return True, ""
    prompt = load_prompt(
        "censor",
        FORBIDDEN=_load_forbidden(),
        CONTENT=content_text[:6000],
        CONTEXT=context or "контент",
    )
    try:
        raw = chat([{"role": "user", "content": prompt}], purpose="censor", json_mode=True)
        data = json.loads(_strip_fences(raw))
        if not isinstance(data, dict):
            raise ValueError("ответ цензора не объект")
    except (LLMError, json.JSONDecodeError, ValueError, TypeError) as exc:
        logger.warning(
            "censor: техническая ошибка проверки (%s) — пропускаем (fail-open)", exc
        )
        return True, ""
    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict == "block":
        return False, str(data.get("reason") or "нарушение правил контента")
    return True, ""
