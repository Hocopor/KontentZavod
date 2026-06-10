"""
Генерация маркетингового профиля проекта с помощью ИИ.

Функция generate_profile:
  - Загружает промпт из app/prompts/profile.txt
  - Вызывает llm.chat (purpose='profile', json_mode=True)
  - Устойчиво парсит JSON-ответ (срезает ```-фенсы, один retry при невалидном JSON)
  - Возвращает dict с 6 ключами: audience, tone, cta, themes, forbidden, extra
  - LLMError пробрасывается без обёртки
"""
import json
import logging
import re

from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt

logger = logging.getLogger(__name__)

# Обязательные ключи профиля
_PROFILE_KEYS = ("audience", "tone", "cta", "themes", "forbidden", "extra")


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_profile_json(raw: str) -> dict:
    """
    Парсит JSON-объект профиля из ответа LLM.
    Возвращает dict с 6 ключами (отсутствующие заменяются на "").
    Бросает ValueError при невалидном JSON или неверной структуре.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)  # может бросить json.JSONDecodeError
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался JSON-объект, получено: {type(data).__name__}")
    # Заполнить отсутствующие ключи пустыми строками
    return {key: str(data.get(key, "")) for key in _PROFILE_KEYS}


def generate_profile(name: str, description: str, goals: str) -> dict:
    """
    Генерирует маркетинговый профиль проекта с помощью ИИ.

    Args:
        name:        Название проекта.
        description: Описание продукта / услуги.
        goals:       Цель продвижения.

    Returns:
        dict с ключами: audience, tone, cta, themes, forbidden, extra.
        Все значения — строки (отсутствующие в ответе LLM → "").

    Raises:
        LLMError: LLM не ответил валидным JSON после retry.
    """
    prompt = load_prompt(
        "profile",
        NAME=name,
        DESCRIPTION=description,
        GOALS=goals,
    )

    messages = [{"role": "user", "content": prompt}]

    # ── Вызов LLM + устойчивый парсинг ───────────────────────────────────────
    raw = chat(messages, purpose="profile", json_mode=True)
    try:
        profile = _parse_profile_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("profile: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка парсинга JSON: {exc}. "
                    "Пожалуйста, ответь ТОЛЬКО валидным JSON-объектом с ключами "
                    "audience, tone, cta, themes, forbidden, extra — без каких-либо пояснений."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="profile", json_mode=True)
        try:
            profile = _parse_profile_json(raw2)
        except (json.JSONDecodeError, ValueError) as exc2:
            raise LLMError(
                f"LLM вернул невалидный JSON профиля после двух попыток: {exc2}"
            ) from exc2

    logger.info("generate_profile: name=%r, ключи=%s", name, list(profile.keys()))
    return profile
