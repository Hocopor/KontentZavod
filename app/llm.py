"""
Клиент LLM-Router для КонтентЗавода.

Все LLM-вызовы идут ТОЛЬКО через LLM-Router (openai SDK, base_url из .env).
Никаких прямых обращений к DeepSeek/Gemini/etc.

────────────────────────────────────────────────────────────
FAKE_LLM=1 (для тестов и локальной разработки без сети):

  purpose='ideas'  → JSON-массив из 5 объектов:
      [
        {"text": "Идея 1: <тема>"},
        ...
      ]

  purpose='script' → JSON с текстами per-платформа:
      {
        "title":    "Заголовок поста",
        "telegram": {
            "text":    "Полный текст поста для Telegram (до 4096 символов)",
            "hashtags": ["#тег1", "#тег2"]
        },
        "vk": {
            "text":    "Текст для VK (до 15000 символов)",
            "hashtags": ["#тег1", "#тег2"]
        },
        "youtube": {
            "title":       "Название Shorts-видео",
            "description": "Описание для YouTube",
            "hashtags":    ["#тег1", "#тег2"]
        },
        "instagram": {
            "caption":  "Подпись для Instagram (до 2200 символов)",
            "hashtags": ["#тег1", "#тег2"]
        },
        "dzen": {
            "title": "Заголовок статьи Дзен",
            "text":  "Текст статьи (поддерживает Markdown)"
        },
        "features": {
            "hook_type":  "вопрос|факт|история|провокация",
            "topic":      "основная тема поста",
            "length":     "short|medium|long",
            "format":     "text|carousel|reel",
            "ab_variant": "A|B|null"
        }
      }

  purpose=None / прочие → строка 'FAKE_LLM response'

────────────────────────────────────────────────────────────
Структура texts JSON в таблице content (для следующих волн):

  Поле content.texts хранит тот же объект, что purpose='script' возвращает
  в ключах "telegram", "vk", "youtube", "instagram", "dzen".
  Поле content.features — JSON из ключа "features" того же объекта.

  Пример доступа в pipeline:
      import json
      texts = json.loads(row["texts"])
      tg_text = texts["telegram"]["text"]
      features = json.loads(row["features"])
      hook = features["hook_type"]
────────────────────────────────────────────────────────────
"""
import json
import logging
from typing import Literal

from openai import OpenAI, APIConnectionError, APIStatusError

from app.config import settings

logger = logging.getLogger(__name__)

# ─── Исключения ───────────────────────────────────────────────────────────────


class LLMError(Exception):
    """Ошибка вызова LLM-Router (сеть, таймаут, 4xx/5xx от роутера)."""


# ─── Заглушки для FAKE_LLM=1 ─────────────────────────────────────────────────

_FAKE_IDEAS = json.dumps(
    [
        {"text": "Идея 1: Как наш продукт решает главную боль клиента за 5 минут"},
        {"text": "Идея 2: 3 мифа о нашей нише, которые мешают людям достичь результата"},
        {"text": "Идея 3: История клиента: с нуля до результата за 30 дней"},
        {"text": "Идея 4: Закулисье: как мы готовим продукт/услугу"},
        {"text": "Идея 5: Сравнение: наш подход vs стандартный рынок"},
    ],
    ensure_ascii=False,
)

_FAKE_SCRIPT = json.dumps(
    {
        "title": "Как мы решаем главную боль клиента",
        "telegram": {
            "text": (
                "🔥 Знаете, что мешает большинству людей получить результат?\n\n"
                "Они ищут сложные решения там, где нужно простое.\n\n"
                "Мы в [Проект] сделали именно это — убрали всё лишнее "
                "и оставили только то, что работает.\n\n"
                "Попробуйте сами 👇"
            ),
            "hashtags": ["#продукт", "#результат", "#кейс"],
        },
        "vk": {
            "text": (
                "Знаете, что мешает большинству людей получить результат?\n\n"
                "Они ищут сложные решения там, где нужно простое.\n\n"
                "Мы в [Проект] сделали именно это — убрали всё лишнее "
                "и оставили только то, что работает.\n\n"
                "Расскажите в комментариях — с какой болью вы сталкивались чаще всего?"
            ),
            "hashtags": ["#продукт", "#результат"],
        },
        "youtube": {
            "title": "Как мы решаем главную боль клиента за 5 минут",
            "description": (
                "В этом видео рассказываем, как [Проект] помогает решить "
                "главную боль клиентов быстро и просто.\n\n"
                "Подписывайтесь на канал!"
            ),
            "hashtags": ["#shorts", "#продукт"],
        },
        "instagram": {
            "caption": (
                "Главная боль клиента — и как мы её решаем 💡\n\n"
                "Простое решение там, где все ищут сложное.\n\n"
                "Ссылка в профиле 👆"
            ),
            "hashtags": ["#продукт", "#результат", "#бизнес"],
        },
        "dzen": {
            "title": "Как мы решаем главную боль клиента: честный разбор",
            "text": (
                "## Постановка проблемы\n\n"
                "Большинство людей ищут сложные решения там, где нужно простое.\n\n"
                "## Наш подход\n\n"
                "Мы убрали всё лишнее и оставили только то, что работает.\n\n"
                "## Результат\n\n"
                "Клиенты получают результат быстрее и с меньшими усилиями."
            ),
        },
        "features": {
            "hook_type": "вопрос",
            "topic": "боль клиента и решение",
            "length": "medium",
            "format": "text",
            "ab_variant": "null",
        },
    },
    ensure_ascii=False,
)

# ─── Основной клиент ──────────────────────────────────────────────────────────


def chat(
    messages: list[dict],
    model: str = "auto",
    purpose: str | None = None,
    json_mode: bool = False,
    max_tokens: int | None = None,
) -> str:
    """
    Отправить сообщения в LLM-Router и получить текстовый ответ.

    Args:
        messages:   список {"role": "user"|"system"|"assistant", "content": "..."}
        model:      "auto" (роутер выбирает) | "deepseek/deepseek-chat" | ...
        purpose:    назначение вызова ('ideas', 'script', ...) — для логирования
                    и выбора заглушки при FAKE_LLM=1
        json_mode:  если True — передаёт response_format=json_object роутеру
        max_tokens: максимум токенов в ответе (None = дефолт модели)

    Returns:
        Строка с ответом модели.

    Raises:
        LLMError: при сетевых ошибках, таймаутах, 4xx/5xx от роутера.
    """
    # ── Режим заглушки ─────────────────────────────────────────────────────────
    if settings.FAKE_LLM:
        logger.debug("FAKE_LLM=1, purpose=%s — возвращаем заглушку", purpose)
        if purpose == "ideas":
            return _FAKE_IDEAS
        if purpose == "script":
            return _FAKE_SCRIPT
        return "FAKE_LLM response"

    # ── Реальный вызов ─────────────────────────────────────────────────────────
    if not settings.LLM_ROUTER_KEY:
        raise LLMError("LLM_ROUTER_KEY не задан в .env")

    client = OpenAI(
        base_url=settings.LLM_ROUTER_BASE_URL,
        api_key=settings.LLM_ROUTER_KEY,
    )

    kwargs: dict = {
        "model": model,
        "messages": messages,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        logger.info("LLM call: purpose=%s model=%s", purpose, model)
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""
    except APIConnectionError as exc:
        raise LLMError(
            f"Не удалось подключиться к LLM-Router ({settings.LLM_ROUTER_BASE_URL}): {exc}"
        ) from exc
    except APIStatusError as exc:
        raise LLMError(
            f"LLM-Router вернул ошибку {exc.status_code}: {exc.message}"
        ) from exc
    except Exception as exc:
        raise LLMError(f"Неожиданная ошибка LLM: {exc}") from exc
