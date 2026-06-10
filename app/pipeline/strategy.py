"""
Пайплайн маркетинговой стратегии (этап 7, волна B) — «сердце Завода 2.0».

build_strategy(strategy_id) обрабатывает строку strategies со status='generating':
  Шаг 0 — собрать inputs (профиль, площадки+типы, learnings, метрики, прошлые темы,
           user_comment, предыдущая стратегия) → сохранить в strategies.inputs (JSON).
  Шаг 1 — аналитическая записка (purpose='strategy_analysis', json_mode=False).
  Шаг 2 — стратегия: если есть прошлая стратегия + user_comment → strategy_revise,
           иначе → strategy (json_mode=True, парсинг с фенсами + 1 retry).
  Шаг 3 — валидация/фильтрация: только включённые площадки и валидные/включённые типы
           в content_mix; если пусто — retry 1 раз, потом error.
  Шаг 4 — сохранить strategy+inputs, status='active', остальные active проекта → archived.

Любая ошибка → status='error', error_text (наружу не бросаем — фоновый джоб не должен падать).
Вызывается джобом process_brain (волна C) и тестами напрямую.
"""
import json
import logging
import re

from app import catalog
from app.db import get_db, get_project_settings
from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt

logger = logging.getLogger(__name__)

# Сколько последних тем подтягивать для свежести (против повторов)
_RECENT_LIMIT = 40

PLATFORMS_ORDER = ["telegram", "vk", "youtube", "instagram", "dzen"]


# ─── Парсинг JSON ─────────────────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_strategy_json(raw: str) -> dict:
    """
    Парсит JSON-объект стратегии. Бросает ValueError при невалидной структуре.
    Минимальные требования: объект с ключом 'platforms' (dict).
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)  # может бросить json.JSONDecodeError
    if not isinstance(data, dict):
        raise ValueError("Стратегия должна быть JSON-объектом")
    platforms = data.get("platforms")
    if not isinstance(platforms, dict):
        raise ValueError("Поле 'platforms' должно быть объектом")
    return data


# ─── Сбор входных данных (шаг 0) ──────────────────────────────────────────────


def _enabled_platforms(db, project_id: int) -> dict[str, dict[str, bool]]:
    """
    Включённые площадки проекта → {platform: {type: enabled_bool}}.

    Типы берём из project_platforms.content_types (JSON), при NULL — default_on
    из каталога. Оставляем только типы, валидные по каталогу.
    """
    rows = db.execute(
        "SELECT platform, content_types FROM project_platforms "
        "WHERE project_id=? AND enabled=1",
        (project_id,),
    ).fetchall()

    result: dict[str, dict[str, bool]] = {}
    for r in rows:
        platform = r["platform"]
        allowed = catalog.allowed_types(platform)
        if not allowed:
            continue
        saved: dict = {}
        if r["content_types"]:
            try:
                saved = json.loads(r["content_types"])
            except (json.JSONDecodeError, TypeError):
                saved = {}
        types: dict[str, bool] = {}
        for ctype, info in allowed.items():
            if ctype in saved:
                types[ctype] = bool(saved[ctype])
            else:
                types[ctype] = bool(info["default_on"])
        result[platform] = types
    # сортировка для стабильности
    return {
        p: result[p]
        for p in sorted(result, key=lambda x: PLATFORMS_ORDER.index(x)
                        if x in PLATFORMS_ORDER else 99)
    }


def _metrics_summary(db, project_id: int) -> list[dict]:
    """
    Простая сводка метрик по платформе+типу: средние views/likes/comments и кол-во
    публикаций. Только если есть опубликованное с метриками.
    """
    sql = """
        SELECT
            s.platform                       AS platform,
            c.type                           AS type,
            COUNT(*)                         AS n,
            AVG(m.views)                     AS avg_views,
            AVG(m.likes)                     AS avg_likes,
            AVG(m.comments)                  AS avg_comments
        FROM schedule s
        JOIN content c ON c.id = s.content_id
        JOIN (
            SELECT m1.*
            FROM metrics m1
            JOIN (
                SELECT schedule_id, MAX(date) AS max_date
                FROM metrics GROUP BY schedule_id
            ) last ON last.schedule_id = m1.schedule_id
                  AND last.max_date    = m1.date
        ) m ON m.schedule_id = s.id
        WHERE c.project_id = ?
          AND s.status IN ('published', 'manual_done')
        GROUP BY s.platform, c.type
        ORDER BY avg_views DESC
    """
    rows = db.execute(sql, (project_id,)).fetchall()
    out = []
    for r in rows:
        out.append({
            "platform": r["platform"],
            "type": r["type"],
            "publications": r["n"],
            "avg_views": round(r["avg_views"] or 0, 1),
            "avg_likes": round(r["avg_likes"] or 0, 1),
            "avg_comments": round(r["avg_comments"] or 0, 1),
        })
    return out


def _top_topics(db, project_id: int) -> list[str]:
    """Топ-темы по средним просмотрам (из features.topic опубликованного)."""
    sql = """
        SELECT c.features AS features, AVG(m.views) AS avg_views
        FROM schedule s
        JOIN content c ON c.id = s.content_id
        JOIN (
            SELECT m1.*
            FROM metrics m1
            JOIN (
                SELECT schedule_id, MAX(date) AS max_date
                FROM metrics GROUP BY schedule_id
            ) last ON last.schedule_id = m1.schedule_id
                  AND last.max_date    = m1.date
        ) m ON m.schedule_id = s.id
        WHERE c.project_id = ?
          AND s.status IN ('published', 'manual_done')
        GROUP BY c.id
        ORDER BY avg_views DESC
        LIMIT 10
    """
    rows = db.execute(sql, (project_id,)).fetchall()
    topics: list[str] = []
    for r in rows:
        try:
            feats = json.loads(r["features"]) if r["features"] else {}
        except (json.JSONDecodeError, TypeError):
            feats = {}
        topic = (feats.get("topic") or "").strip()
        if topic and topic not in topics:
            topics.append(topic)
    return topics


def _past_topics(db, project_id: int) -> list[str]:
    """Темы прошлых идей и plan_items (против повторов)."""
    topics: list[str] = []
    idea_rows = db.execute(
        "SELECT text FROM ideas WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
        (project_id, _RECENT_LIMIT),
    ).fetchall()
    for r in idea_rows:
        t = (r["text"] or "").strip()[:120]
        if t:
            topics.append(t)
    plan_rows = db.execute(
        "SELECT title FROM plan_items WHERE project_id=? ORDER BY created_at DESC LIMIT ?",
        (project_id, _RECENT_LIMIT),
    ).fetchall()
    for r in plan_rows:
        t = (r["title"] or "").strip()[:120]
        if t:
            topics.append(t)
    return topics


def _previous_strategy(db, project_id: int, exclude_id: int) -> dict | None:
    """
    Предыдущая стратегия: активная, иначе последняя архивная.
    Возвращает {version, status, strategy: dict} или None.
    """
    row = db.execute(
        "SELECT id, version, status, strategy FROM strategies "
        "WHERE project_id=? AND id!=? AND status IN ('active','archived') "
        "ORDER BY (status='active') DESC, version DESC LIMIT 1",
        (project_id, exclude_id),
    ).fetchone()
    if row is None:
        return None
    try:
        strat = json.loads(row["strategy"]) if row["strategy"] else {}
    except (json.JSONDecodeError, TypeError):
        strat = {}
    return {"version": row["version"], "status": row["status"], "strategy": strat}


def _collect_inputs(db, project, strategy_row) -> dict:
    """Собрать все входные данные для анализа и генерации стратегии (шаг 0)."""
    project_id = project["id"]

    learning_rows = db.execute(
        "SELECT insight, weight FROM learnings "
        "WHERE project_id=? AND active=1 ORDER BY weight DESC",
        (project_id,),
    ).fetchall()
    learnings = [{"insight": r["insight"], "weight": r["weight"]} for r in learning_rows]

    inputs = {
        "profile": {
            "name": project["name"] or "",
            "description": project["description"] or "",
            "audience": project["audience"] or "",
            "tone": project["tone"] or "",
            "goals": project["goals"] or "",
            "cta": project["cta"] or "",
            "themes": project["themes"] or "",
            "forbidden": project["forbidden"] or "",
            "extra": project["extra"] or "",
        },
        "settings": get_project_settings(project["settings"]),
        "platforms": _enabled_platforms(db, project_id),
        "learnings": learnings,
        "metrics": _metrics_summary(db, project_id),
        "top_topics": _top_topics(db, project_id),
        "past_topics": _past_topics(db, project_id),
        "user_comment": (strategy_row["user_comment"] or "").strip(),
        "previous_strategy": _previous_strategy(db, project_id, strategy_row["id"]),
    }
    return inputs


# ─── Рендер блоков для промптов ───────────────────────────────────────────────


def _platforms_block(platforms: dict[str, dict[str, bool]]) -> str:
    """Текстовое описание включённых площадок и включённых типов."""
    if not platforms:
        return "Пока ни одной площадки не подключено."
    lines = []
    for platform, types in platforms.items():
        enabled_types = [t for t, on in types.items() if on]
        labels = []
        for t in enabled_types:
            info = catalog.type_info(platform, t)
            labels.append(f"{t} ({info['label']})" if info else t)
        lines.append(f"• {platform}: {', '.join(labels) if labels else 'нет включённых типов'}")
    return "\n".join(lines)


def _learnings_block(learnings: list[dict]) -> str:
    if not learnings:
        return "Пока нет — это первая стратегия проекта."
    return "\n".join(f"• {l['insight']}" for l in learnings)


def _metrics_block(metrics: list[dict]) -> str:
    if not metrics:
        return "Публикаций с метриками пока нет."
    lines = ["площадка | тип | публикаций | ср.просмотры | ср.лайки | ср.комменты"]
    for m in metrics:
        lines.append(
            f"{m['platform']} | {m['type']} | {m['publications']} | "
            f"{m['avg_views']} | {m['avg_likes']} | {m['avg_comments']}"
        )
    return "\n".join(lines)


def _topics_block(top_topics: list[str], past_topics: list[str]) -> str:
    parts = []
    if top_topics:
        parts.append("Лучшие по просмотрам темы:\n" + "\n".join(f"• {t}" for t in top_topics))
    if past_topics:
        parts.append("Уже было (не повторять дословно):\n"
                     + "\n".join(f"• {t}" for t in past_topics[:30]))
    return "\n\n".join(parts) if parts else "Пока нет выпущенного контента."


# ─── Шаги 1–2: LLM ────────────────────────────────────────────────────────────


def _analysis_note(inputs: dict) -> str:
    """Шаг 1 — аналитическая записка (текст)."""
    p = inputs["profile"]
    prompt = load_prompt(
        "strategy_analysis",
        PROJECT_NAME=p["name"],
        PROJECT_DESCRIPTION=p["description"],
        PROJECT_AUDIENCE=p["audience"],
        PROJECT_TONE=p["tone"],
        PROJECT_GOALS=p["goals"],
        PROJECT_CTA=p["cta"],
        PROJECT_THEMES=p["themes"],
        PROJECT_FORBIDDEN=p["forbidden"],
        PROJECT_EXTRA=p["extra"],
        PLATFORMS_BLOCK=_platforms_block(inputs["platforms"]),
        LEARNINGS_BLOCK=_learnings_block(inputs["learnings"]),
        METRICS_BLOCK=_metrics_block(inputs["metrics"]),
        PAST_TOPICS_BLOCK=_topics_block(inputs["top_topics"], inputs["past_topics"]),
        USER_COMMENT_BLOCK=inputs["user_comment"] or "Не задан.",
    )
    return chat([{"role": "user", "content": prompt}],
                purpose="strategy_analysis", json_mode=False)


def _generate_strategy(inputs: dict, note: str) -> dict:
    """
    Шаг 2 — стратегия (JSON). Выбор purpose: revise если есть прошлая
    стратегия И user_comment, иначе обычная strategy. Парсинг + 1 retry.
    """
    p = inputs["profile"]
    platforms_block = _platforms_block(inputs["platforms"])
    prev = inputs["previous_strategy"]
    is_revise = bool(prev and prev.get("strategy") and inputs["user_comment"])

    if is_revise:
        purpose = "strategy_revise"
        prompt = load_prompt(
            "strategy_revise",
            ANALYSIS_NOTE=note,
            PREVIOUS_STRATEGY=json.dumps(prev["strategy"], ensure_ascii=False, indent=2),
            USER_COMMENT=inputs["user_comment"],
            METRICS_BLOCK=_metrics_block(inputs["metrics"]),
            PLATFORMS_BLOCK=platforms_block,
        )
    else:
        purpose = "strategy"
        prompt = load_prompt(
            "strategy",
            ANALYSIS_NOTE=note,
            PROJECT_NAME=p["name"],
            PROJECT_DESCRIPTION=p["description"],
            PROJECT_AUDIENCE=p["audience"],
            PROJECT_GOALS=p["goals"],
            PROJECT_CTA=p["cta"],
            PLATFORMS_BLOCK=platforms_block,
        )

    messages = [{"role": "user", "content": prompt}]
    raw = chat(messages, purpose=purpose, json_mode=True)
    try:
        return _parse_strategy_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("strategy: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка парсинга JSON: {exc}. Ответь ТОЛЬКО валидным JSON-объектом "
                    "стратегии с ключами summary, positioning, platforms, без пояснений "
                    "и без markdown-фенсов."
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose=purpose, json_mode=True)
        return _parse_strategy_json(raw2)


# ─── Шаг 3: фильтрация ────────────────────────────────────────────────────────


def _filter_strategy(strategy: dict, platforms: dict[str, dict[str, bool]]) -> dict:
    """
    Оставить в platforms только включённые площадки; в content_mix — только типы,
    валидные по каталогу И включённые у площадки. Бросает ValueError, если после
    фильтрации не осталось ни одной площадки.
    """
    raw_platforms = strategy.get("platforms", {})
    enabled_types_map = {
        p: {t for t, on in types.items() if on}
        for p, types in platforms.items()
    }

    filtered: dict = {}
    for platform, pdata in raw_platforms.items():
        if platform not in enabled_types_map:
            continue
        if not isinstance(pdata, dict):
            continue
        allowed = enabled_types_map[platform]
        pdata = dict(pdata)
        mix = pdata.get("content_mix", {})
        if isinstance(mix, dict):
            clean_mix = {}
            for ctype, n in mix.items():
                if ctype in allowed and catalog.is_valid(platform, ctype):
                    clean_mix[ctype] = n
            pdata["content_mix"] = clean_mix
        else:
            pdata["content_mix"] = {}
        filtered[platform] = pdata

    if not filtered:
        raise ValueError("После фильтрации не осталось ни одной включённой площадки")

    result = dict(strategy)
    result["platforms"] = filtered
    return result


# ─── Основная функция ─────────────────────────────────────────────────────────


def _set_error(strategy_id: int, message: str) -> None:
    """Перевести стратегию в status='error' с текстом ошибки."""
    try:
        with get_db() as db:
            db.execute(
                "UPDATE strategies SET status='error', error_text=? WHERE id=?",
                (message[:2000], strategy_id),
            )
    except Exception:  # noqa: BLE001 — даже запись ошибки не должна валить джоб
        logger.exception("Не удалось записать error для strategy_id=%d", strategy_id)


def build_strategy(strategy_id: int) -> None:
    """
    Построить стратегию для строки strategies со status='generating'.

    Не бросает наружу: любая ошибка → status='error', error_text.
    """
    try:
        with get_db() as db:
            strategy_row = db.execute(
                "SELECT * FROM strategies WHERE id=?", (strategy_id,)
            ).fetchone()
            if strategy_row is None:
                logger.warning("build_strategy: strategy_id=%d не найдена", strategy_id)
                return
            if strategy_row["status"] != "generating":
                logger.info(
                    "build_strategy: strategy_id=%d status=%s — пропуск",
                    strategy_id, strategy_row["status"],
                )
                return
            project = db.execute(
                "SELECT * FROM projects WHERE id=?", (strategy_row["project_id"],)
            ).fetchone()
            if project is None:
                _set_error(strategy_id, "Проект не найден")
                return

            # Шаг 0 — сбор inputs
            inputs = _collect_inputs(db, project, strategy_row)

        # Сохраним inputs сразу (на случай падения LLM — видно, с чем работали)
        with get_db() as db:
            db.execute(
                "UPDATE strategies SET inputs=? WHERE id=?",
                (json.dumps(inputs, ensure_ascii=False), strategy_id),
            )

        if not inputs["platforms"]:
            _set_error(strategy_id, "У проекта нет ни одной включённой площадки")
            return

        # Шаг 1 — аналитическая записка
        note = _analysis_note(inputs)

        # Шаг 2 — стратегия (+ retry внутри)
        strategy = _generate_strategy(inputs, note)

        # Шаг 3 — фильтрация; при пустом результате — ещё один полный retry шага 2
        try:
            strategy = _filter_strategy(strategy, inputs["platforms"])
        except ValueError:
            logger.warning(
                "strategy_id=%d: пустой platforms после фильтрации — retry генерации",
                strategy_id,
            )
            strategy = _generate_strategy(inputs, note)
            strategy = _filter_strategy(strategy, inputs["platforms"])

        # Записку приложим к стратегии для прозрачности
        strategy.setdefault("analysis_note", note)

        # Шаг 4 — сохранение + архивация остальных active
        project_id = project["id"]
        with get_db() as db:
            db.execute(
                "UPDATE strategies SET status='archived' "
                "WHERE project_id=? AND status='active' AND id!=?",
                (project_id, strategy_id),
            )
            db.execute(
                "UPDATE strategies SET status='active', strategy=?, error_text=NULL "
                "WHERE id=?",
                (json.dumps(strategy, ensure_ascii=False), strategy_id),
            )
        logger.info("build_strategy: strategy_id=%d → active", strategy_id)

    except LLMError as exc:
        logger.warning("build_strategy: strategy_id=%d LLMError: %s", strategy_id, exc)
        _set_error(strategy_id, f"Ошибка LLM: {exc}")
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("build_strategy: strategy_id=%d невалидный ответ: %s", strategy_id, exc)
        _set_error(strategy_id, f"Невалидный ответ модели: {exc}")
    except Exception as exc:  # noqa: BLE001 — джоб не должен падать
        logger.exception("build_strategy: strategy_id=%d неожиданная ошибка", strategy_id)
        _set_error(strategy_id, f"Неожиданная ошибка: {exc}")
