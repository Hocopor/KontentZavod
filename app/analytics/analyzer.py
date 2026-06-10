"""
Еженедельный LLM-анализ контента → инсайты в таблицу learnings.

«Мозг» самообучения завода: коррелирует features опубликованного контента
(hook_type, topic, length, format, ab_variant) с метриками (просмотры, лайки,
комменты, шеры, watch_pct) и формулирует применимые при генерации инсайты.

Инсайты пишутся в learnings (source='analyzer'); они автоматически
подмешиваются в промпты генерации (см. pipeline/ideas.py, script.py).

Вызывается из APScheduler еженедельно (джоб добавляет scheduler.py отдельно).
"""
import json
import logging
import re

from app.db import get_db
from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt

logger = logging.getLogger(__name__)

# Минимум публикаций с метриками, чтобы вообще звать LLM
_MIN_PUBLICATIONS = 5
# Максимум новых инсайтов за один прогон
_MAX_NEW_INSIGHTS = 5
# Максимум активных analyzer-инсайтов на проект
_MAX_ACTIVE_INSIGHTS = 10
# Границы веса
_WEIGHT_MIN = 0.5
_WEIGHT_MAX = 2.0


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _clamp_weight(value) -> float:
    """Привести вес к диапазону [0.5, 2.0]; нечисловое → 1.0."""
    try:
        w = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(_WEIGHT_MIN, min(_WEIGHT_MAX, w))


def _parse_analyze_json(raw: str) -> dict:
    """
    Парсит JSON-ответ аналитика.
    Бросает ValueError при невалидном JSON или структуре.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Ответ должен быть JSON-объектом")
    insights = data.get("insights")
    if not isinstance(insights, list):
        raise ValueError("Поле 'insights' должно быть списком")
    for item in insights:
        if not isinstance(item, dict) or not item.get("insight"):
            raise ValueError("Каждый insight должен быть объектом с непустым 'insight'")
    deactivate = data.get("deactivate", [])
    if not isinstance(deactivate, list):
        raise ValueError("Поле 'deactivate' должно быть списком")
    return data


def _fetch_publications(project_id: int) -> list[dict]:
    """
    Опубликованные публикации проекта с последним замером метрик,
    объединённые с features контента.
    """
    sql = """
        SELECT
            s.platform                              AS platform,
            c.type                                  AS type,
            c.features                              AS features,
            m.views, m.likes, m.comments, m.shares, m.watch_pct
        FROM schedule s
        JOIN content c ON c.id = s.content_id
        JOIN (
            -- последний по дате замер метрик на каждую публикацию
            SELECT m1.*
            FROM metrics m1
            JOIN (
                SELECT schedule_id, MAX(date) AS max_date
                FROM metrics
                GROUP BY schedule_id
            ) last ON last.schedule_id = m1.schedule_id
                  AND last.max_date    = m1.date
        ) m ON m.schedule_id = s.id
        WHERE c.project_id = ?
          AND s.status IN ('published', 'manual_done')
        ORDER BY s.id
    """
    with get_db() as db:
        rows = db.execute(sql, (project_id,)).fetchall()

    pubs: list[dict] = []
    for r in rows:
        try:
            features = json.loads(r["features"]) if r["features"] else {}
        except (json.JSONDecodeError, TypeError):
            features = {}
        pubs.append(
            {
                "platform": r["platform"],
                "type": r["type"],
                "hook_type": features.get("hook_type", ""),
                "topic": features.get("topic", ""),
                "length": features.get("length", ""),
                "format": features.get("format", ""),
                "ab_variant": features.get("ab_variant", ""),
                "views": r["views"],
                "likes": r["likes"],
                "comments": r["comments"],
                "shares": r["shares"],
                "watch_pct": r["watch_pct"],
            }
        )
    return pubs


def _render_table(pubs: list[dict]) -> str:
    """Превратить публикации в текстовую таблицу для промпта."""
    lines = []
    for p in pubs:
        lines.append(
            f"{p['platform']} | {p['type']} | {p['hook_type']} | {p['topic']} | "
            f"{p['length']} | {p['format']} | {p['ab_variant']} | "
            f"{p['views']} | {p['likes']} | {p['comments']} | "
            f"{p['shares']} | {p['watch_pct']}"
        )
    return "\n".join(lines)


def analyze_project(project_id: int) -> dict:
    """
    Проанализировать один проект и записать инсайты в learnings.

    Returns:
        {"skipped": "..."}                          — если данных мало;
        {"insights_added": n, "deactivated": n}     — при успехе.

    Raises:
        LLMError: если LLM дважды вернул невалидный JSON.
    """
    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
    if project is None:
        return {"skipped": "проект не найден"}

    pubs = _fetch_publications(project_id)
    if len(pubs) < _MIN_PUBLICATIONS:
        logger.info(
            "analyze_project(%d): публикаций с метриками %d < %d — пропуск",
            project_id, len(pubs), _MIN_PUBLICATIONS,
        )
        return {"skipped": "недостаточно данных"}

    # Уже существующие активные analyzer-инсайты
    with get_db() as db:
        existing_rows = db.execute(
            "SELECT insight FROM learnings "
            "WHERE project_id=? AND active=1 AND source='analyzer' "
            "ORDER BY weight DESC",
            (project_id,),
        ).fetchall()
    existing_insights = [r["insight"] for r in existing_rows]
    existing_text = (
        "\n".join(f"• {ins}" for ins in existing_insights)
        if existing_insights
        else "Пока нет."
    )

    prompt = load_prompt(
        "analyze",
        PROJECT_NAME=project["name"] or "",
        PROJECT_DESCRIPTION=project["description"] or "",
        PROJECT_AUDIENCE=project["audience"] or "",
        PUBLICATIONS_TABLE=_render_table(pubs),
        EXISTING_LEARNINGS=existing_text,
    )
    messages = [{"role": "user", "content": prompt}]

    # ── Вызов LLM + 1 retry ──────────────────────────────────────────────────
    raw = chat(messages, purpose="analyze", json_mode=True)
    try:
        data = _parse_analyze_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("analyze: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка: {exc}. Ответь ТОЛЬКО валидным JSON-объектом строго "
                    "по формату: {\"insights\": [{\"insight\": \"...\", "
                    "\"weight\": 1.0}], \"deactivate\": [...]}."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="analyze", json_mode=True)
        try:
            data = _parse_analyze_json(raw2)
        except (json.JSONDecodeError, ValueError) as exc2:
            raise LLMError(
                f"LLM вернул невалидный JSON анализа после двух попыток: {exc2}"
            ) from exc2

    insights = data.get("insights", [])[:_MAX_NEW_INSIGHTS]
    deactivate = data.get("deactivate", [])

    added = 0
    deactivated = 0
    with get_db() as db:
        # ── deactivate: только analyzer-инсайты, reject не трогаем ───────────
        for text in deactivate:
            if not isinstance(text, str) or not text.strip():
                continue
            cur = db.execute(
                "UPDATE learnings SET active=0 "
                "WHERE project_id=? AND insight=? AND source='analyzer' AND active=1",
                (project_id, text),
            )
            deactivated += cur.rowcount

        # ── insert новых инсайтов с дедупом ──────────────────────────────────
        existing_set = set(existing_insights)
        for item in insights:
            insight_text = (item.get("insight") or "").strip()
            if not insight_text or insight_text in existing_set:
                continue
            # Дедуп по БД: точно такой insight уже есть у проекта (любой статус/источник)?
            dup = db.execute(
                "SELECT 1 FROM learnings WHERE project_id=? AND insight=? LIMIT 1",
                (project_id, insight_text),
            ).fetchone()
            if dup is not None:
                continue
            weight = _clamp_weight(item.get("weight"))
            db.execute(
                "INSERT INTO learnings (project_id, insight, source, weight, active) "
                "VALUES (?, ?, 'analyzer', ?, 1)",
                (project_id, insight_text, weight),
            )
            existing_set.add(insight_text)
            added += 1

        # ── Ограничение: ≤ 10 активных analyzer-инсайтов на проект ───────────
        active_rows = db.execute(
            "SELECT id FROM learnings "
            "WHERE project_id=? AND active=1 AND source='analyzer' "
            "ORDER BY weight DESC, id DESC",
            (project_id,),
        ).fetchall()
        if len(active_rows) > _MAX_ACTIVE_INSIGHTS:
            # деактивируем «хвост» — с наименьшим весом
            for row in active_rows[_MAX_ACTIVE_INSIGHTS:]:
                db.execute(
                    "UPDATE learnings SET active=0 WHERE id=?", (row["id"],)
                )

    logger.info(
        "analyze_project(%d): добавлено %d, деактивировано %d",
        project_id, added, deactivated,
    )
    return {"insights_added": added, "deactivated": deactivated}


def analyze_all() -> dict:
    """
    Проанализировать все активные проекты. Ошибка одного (LLMError)
    не валит остальные.

    Returns:
        {"projects": n, "insights_added": n, "deactivated": n, "skipped": n}
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM projects WHERE status='active' ORDER BY id"
        ).fetchall()
    project_ids = [r["id"] for r in rows]

    insights_added = 0
    deactivated = 0
    skipped = 0
    for pid in project_ids:
        try:
            result = analyze_project(pid)
        except LLMError as exc:
            logger.warning("analyze_all: проект %d упал с LLMError: %s", pid, exc)
            continue
        if "skipped" in result:
            skipped += 1
        else:
            insights_added += result.get("insights_added", 0)
            deactivated += result.get("deactivated", 0)

    return {
        "projects": len(project_ids),
        "insights_added": insights_added,
        "deactivated": deactivated,
        "skipped": skipped,
    }
