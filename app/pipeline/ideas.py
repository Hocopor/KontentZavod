"""
Генерация идей для контент-плана проекта.

Функция generate_ideas:
  - Собирает профиль проекта, активные learnings и недавние темы из БД
  - Строит промпт через app/pipeline/prompts.py
  - Вызывает llm.chat (purpose='ideas', json_mode=True)
  - Устойчиво парсит JSON-ответ (срезает ```-фенсы, один retry при невалидном JSON)
  - Сохраняет идеи в таблицу ideas (source='generated', status='new')
  - Возвращает список id созданных идей
"""
import json
import logging
import re

from app.db import get_db
from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt

logger = logging.getLogger(__name__)

# Сколько последних тем подтягивать для защиты от повторов
_RECENT_LIMIT = 30


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    # Убрать открывающий фенс (```json или ```)
    text = re.sub(r"^```(?:json)?\s*", "", text)
    # Убрать закрывающий фенс
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_ideas_json(raw: str) -> list[dict]:
    """
    Парсит JSON-массив идей из ответа LLM.
    Возвращает список dict с ключом 'text'.
    Бросает ValueError при невалидном JSON или неверной структуре.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)  # может бросить json.JSONDecodeError
    if not isinstance(data, list):
        raise ValueError(f"Ожидался JSON-массив, получено: {type(data).__name__}")
    ideas = []
    for item in data:
        if not isinstance(item, dict) or "text" not in item:
            raise ValueError(f"Элемент массива должен быть объектом с ключом 'text': {item!r}")
        ideas.append(item)
    return ideas


def generate_ideas(project_id: int, n: int = 5) -> list[int]:
    """
    Генерирует n идей для проекта project_id.

    Args:
        project_id: ID проекта в БД.
        n:          Количество идей (по умолчанию 5).

    Returns:
        Список id созданных записей в таблице ideas.

    Raises:
        ValueError:  Проект не найден или заархивирован.
        LLMError:    LLM не ответил валидным JSON после retry.
    """
    # ── 1. Загрузить профиль проекта ──────────────────────────────────────────
    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()

    if project is None:
        raise ValueError(f"Проект с id={project_id} не найден")
    if project["status"] == "archived":
        raise ValueError(f"Проект «{project['name']}» заархивирован — генерация невозможна")

    # ── 2. Активные learnings ─────────────────────────────────────────────────
    with get_db() as db:
        learning_rows = db.execute(
            "SELECT insight FROM learnings WHERE project_id=? AND active=1 ORDER BY weight DESC",
            (project_id,),
        ).fetchall()

    learnings_text = (
        "\n".join(f"• {r['insight']}" for r in learning_rows)
        if learning_rows
        else "Пока нет — это первые генерации проекта."
    )

    # ── 3. Последние темы (защита от повторов) ────────────────────────────────
    with get_db() as db:
        recent_rows = db.execute(
            """
            SELECT text FROM ideas
            WHERE project_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (project_id, _RECENT_LIMIT),
        ).fetchall()

    recent_text = (
        "\n".join(f"• {r['text'][:120]}" for r in recent_rows)
        if recent_rows
        else "Пока нет — это первые идеи."
    )

    # ── 4. Построить промпт ───────────────────────────────────────────────────
    prompt = load_prompt(
        "ideas",
        PROJECT_NAME=project["name"] or "",
        PROJECT_DESCRIPTION=project["description"] or "",
        PROJECT_AUDIENCE=project["audience"] or "",
        PROJECT_TONE=project["tone"] or "",
        PROJECT_GOALS=project["goals"] or "",
        PROJECT_CTA=project["cta"] or "",
        PROJECT_THEMES=project["themes"] or "",
        PROJECT_FORBIDDEN=project["forbidden"] or "",
        PROJECT_EXTRA=project["extra"] or "",
        PROJECT_LEARNINGS=learnings_text,
        RECENT_TOPICS=recent_text,
        N=str(n),
    )

    messages = [{"role": "user", "content": prompt}]

    # ── 5. Вызов LLM + устойчивый парсинг ────────────────────────────────────
    raw = chat(messages, purpose="ideas", json_mode=True)
    try:
        ideas_data = _parse_ideas_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("ideas: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка парсинга JSON: {exc}. "
                    "Пожалуйста, ответь ТОЛЬКО валидным JSON-массивом объектов "
                    "с ключом 'text', без каких-либо пояснений."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="ideas", json_mode=True)
        try:
            ideas_data = _parse_ideas_json(raw2)
        except (json.JSONDecodeError, ValueError) as exc2:
            raise LLMError(
                f"LLM вернул невалидный JSON идей после двух попыток: {exc2}"
            ) from exc2

    # ── 6. Сохранить в БД ─────────────────────────────────────────────────────
    idea_ids: list[int] = []
    with get_db() as db:
        for item in ideas_data:
            text = item["text"].strip()
            if not text:
                continue
            cur = db.execute(
                "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
                (project_id, text, "generated", "new"),
            )
            idea_ids.append(cur.lastrowid)

    logger.info("generate_ideas: project_id=%d, создано %d идей", project_id, len(idea_ids))
    return idea_ids
