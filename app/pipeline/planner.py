"""
Генератор контент-плана по маркетинговой стратегии (этап 7, волна C).

generate_plan(project_id, platform, date_from, date_to) -> int:
  - Берёт активную стратегию проекта (status='active', максимальная версия)
  - Извлекает секцию platforms[platform] из JSON стратегии
  - Определяет включённые типы контента платформы (project_platforms.content_types,
    NULL → default_on из каталога)
  - Собирает активные learnings и последние ~50 тем (plan_items + content)
  - Вызывает LLM (purpose='plan', json_mode=True) с 1 retry при невалидном JSON
  - Валидирует каждый item: дата в периоде, content_type валиден и включён,
    title непустой; time дефолт '12:00'
  - Дедуплицирует: не вставлять дубли по (date, platform, content_type, title)
  - Вставляет plan_items и возвращает число вставленных строк
  - LLMError пробрасывает наружу (ловит вызывающий brain.py)
"""
import json
import logging
import re
from datetime import date as _date

from app import catalog
from app.db import get_db, get_project_settings
from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt
from app.services.cleanup import delete_content_cascade

logger = logging.getLogger(__name__)

# Сколько последних тем подтягивать против повторов
_USED_TOPICS_LIMIT = 50


# ─── Перегенерация контент-плана ──────────────────────────────────────────────


def refresh_plan(
    project_id: int,
    platform: str | None = None,
    content_type: str | None = None,
) -> dict:
    """
    Управляемая перегенерация контент-плана проекта.

    Удаляет plan_items со статусами 'proposed', 'rejected', 'error' в окне
    планирования [сегодня; сегодня+plan_horizon_days], с фильтрацией по
    platform и content_type, затем вызывает generate_plan для затронутых
    платформ.

    Для error-пунктов с content_id выполняется каскадное удаление:
    DELETE schedule → DELETE content → файлы → DELETE plan_item.

    После обновления остаются только: approved, generating, generated.

    Args:
        project_id:   ID проекта.
        platform:     Если задан — ограничить область одной платформой.
        content_type: Если задан — ограничить область одним типом контента.

    Returns:
        Словарь {
            "deleted": int,
            "created": int,
            "platforms": [str, ...],
            "error": str | None,
        }
    """
    from datetime import date as _date_cls, timedelta

    today = _date_cls.today()

    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            return {"deleted": 0, "created": 0, "platforms": [], "error": "Проект не найден"}

        settings = get_project_settings(project["settings"])
        horizon_days: int = int(settings.get("plan_horizon_days", 30))
        date_from_str = today.isoformat()
        date_to_str = (today + timedelta(days=horizon_days)).isoformat()

        # Активная стратегия
        strategy_row = _get_active_strategy(db, project_id)
        if strategy_row is None:
            return {
                "deleted": 0, "created": 0, "platforms": [],
                "error": "Нет активной стратегии — создайте стратегию перед обновлением плана",
            }

        # Определить, какие платформы затронуты
        if platform is not None:
            platforms_to_regen = [platform]
        else:
            # Все включённые платформы проекта
            pp_rows = db.execute(
                "SELECT platform FROM project_platforms WHERE project_id=? AND enabled=1",
                (project_id,),
            ).fetchall()
            platforms_to_regen = [r["platform"] for r in pp_rows]

        if not platforms_to_regen:
            return {"deleted": 0, "created": 0, "platforms": [], "error": None}

        # ── Собрать content_id error-пунктов для каскадного удаления ─────────
        # (у error-пунктов может быть content_id — его нужно удалить каскадом
        #  ПЕРЕД удалением plan_items, пока есть открытое соединение)
        if content_type is not None:
            error_rows = db.execute(
                """
                SELECT content_id FROM plan_items
                WHERE project_id=? AND status='error' AND content_id IS NOT NULL
                  AND platform=? AND content_type=?
                  AND date >= ? AND date <= ?
                """,
                (project_id, platform, content_type, date_from_str, date_to_str),
            ).fetchall()
        elif platform is not None:
            error_rows = db.execute(
                """
                SELECT content_id FROM plan_items
                WHERE project_id=? AND status='error' AND content_id IS NOT NULL
                  AND platform=?
                  AND date >= ? AND date <= ?
                """,
                (project_id, platform, date_from_str, date_to_str),
            ).fetchall()
        else:
            error_rows = db.execute(
                """
                SELECT content_id FROM plan_items
                WHERE project_id=? AND status='error' AND content_id IS NOT NULL
                  AND date >= ? AND date <= ?
                """,
                (project_id, date_from_str, date_to_str),
            ).fetchall()

        error_content_ids = [r["content_id"] for r in error_rows]

        # Каскадно удалить schedule + content для error-пунктов
        for cid in error_content_ids:
            delete_content_cascade(db, cid)

        # Удалить proposed/rejected/error-пункты в окне планирования
        if content_type is not None:
            db.execute(
                """
                DELETE FROM plan_items
                WHERE project_id=? AND status IN ('proposed', 'rejected', 'error')
                  AND platform=? AND content_type=?
                  AND date >= ? AND date <= ?
                """,
                (project_id, platform, content_type, date_from_str, date_to_str),
            )
        elif platform is not None:
            db.execute(
                """
                DELETE FROM plan_items
                WHERE project_id=? AND status IN ('proposed', 'rejected', 'error')
                  AND platform=?
                  AND date >= ? AND date <= ?
                """,
                (project_id, platform, date_from_str, date_to_str),
            )
        else:
            db.execute(
                """
                DELETE FROM plan_items
                WHERE project_id=? AND status IN ('proposed', 'rejected', 'error')
                  AND date >= ? AND date <= ?
                """,
                (project_id, date_from_str, date_to_str),
            )
        deleted = db.execute("SELECT changes() as c").fetchone()["c"]

    # Генерация плана для каждой затронутой платформы
    total_created = 0
    try:
        for plat in platforms_to_regen:
            count = generate_plan_filtered(
                project_id=project_id,
                platform=plat,
                date_from=date_from_str,
                date_to=date_to_str,
                only_content_type=content_type,
            )
            total_created += count
    except LLMError as exc:
        return {
            "deleted": deleted,
            "created": total_created,
            "platforms": platforms_to_regen,
            "error": f"Ошибка LLM: {exc}",
        }

    return {
        "deleted": deleted,
        "created": total_created,
        "platforms": platforms_to_regen,
        "error": None,
    }


# ─── Утилиты парсинга ─────────────────────────────────────────────────────────


def _strip_fences(text: str) -> str:
    """Убрать ```json ... ``` или ``` ... ``` вокруг JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_plan_json(raw: str) -> list[dict]:
    """
    Парсить JSON-объект плана: {"items": [...]}.
    Возвращает список item-словарей.
    Бросает ValueError или json.JSONDecodeError при невалидной структуре.
    """
    cleaned = _strip_fences(raw)
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError(f"Ожидался JSON-объект, получено: {type(data).__name__}")
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError("Поле 'items' должно быть массивом")
    return items


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _get_active_strategy(db, project_id: int) -> dict | None:
    """
    Вернуть строку активной стратегии проекта (status='active', max version).
    None если нет активной стратегии.
    """
    row = db.execute(
        "SELECT * FROM strategies WHERE project_id=? AND status='active' "
        "ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    return row


def _get_enabled_types(db, project_id: int, platform: str) -> dict[str, bool]:
    """
    Вернуть {type: enabled} для платформы проекта.
    При NULL content_types — использует default_on из каталога.
    Возвращает пустой dict если платформа не включена или не найдена.
    """
    row = db.execute(
        "SELECT enabled, content_types FROM project_platforms "
        "WHERE project_id=? AND platform=?",
        (project_id, platform),
    ).fetchone()
    if row is None or not row["enabled"]:
        return {}

    allowed = catalog.allowed_types(platform)
    if not allowed:
        return {}

    saved: dict = {}
    if row["content_types"]:
        try:
            saved = json.loads(row["content_types"])
        except (json.JSONDecodeError, TypeError):
            saved = {}

    result: dict[str, bool] = {}
    for ctype, info in allowed.items():
        if ctype in saved:
            result[ctype] = bool(saved[ctype])
        else:
            result[ctype] = bool(info["default_on"])
    return result


def _get_learnings(db, project_id: int) -> str:
    """Активные learnings проекта в виде текста."""
    rows = db.execute(
        "SELECT insight FROM learnings WHERE project_id=? AND active=1 "
        "ORDER BY weight DESC",
        (project_id,),
    ).fetchall()
    if not rows:
        return "Пока нет активных инсайтов."
    return "\n".join(f"• {r['insight']}" for r in rows)


def _get_used_topics(db, project_id: int, platform: str) -> str:
    """
    Темы последних ~50 plan_items и content проекта (против повторов).
    Для plan_items берём только для данной платформы (если платформа задана).
    """
    topics: list[str] = []

    # Темы из plan_items данной платформы
    plan_rows = db.execute(
        "SELECT title FROM plan_items WHERE project_id=? AND platform=? "
        "ORDER BY created_at DESC LIMIT ?",
        (project_id, platform, _USED_TOPICS_LIMIT),
    ).fetchall()
    for r in plan_rows:
        t = (r["title"] or "").strip()[:120]
        if t and t not in topics:
            topics.append(t)

    # Темы из уже созданного контента проекта
    content_rows = db.execute(
        "SELECT title FROM content WHERE project_id=? "
        "ORDER BY created_at DESC LIMIT ?",
        (project_id, _USED_TOPICS_LIMIT),
    ).fetchall()
    for r in content_rows:
        t = (r["title"] or "").strip()[:120]
        if t and t not in topics:
            topics.append(t)

    if not topics:
        return "Нет — это первый план для этой платформы."
    return "\n".join(f"• {t}" for t in topics[:_USED_TOPICS_LIMIT])


def _content_types_block(platform: str, enabled_types: dict[str, bool]) -> str:
    """Текстовый блок включённых типов с описаниями."""
    lines = []
    for ctype, enabled in enabled_types.items():
        if not enabled:
            continue
        info = catalog.type_info(platform, ctype)
        if info:
            lines.append(f"• {ctype} — {info['label']} (kind: {info['kind']})")
    return "\n".join(lines) if lines else "Нет включённых типов."


def _profile_block(project) -> str:
    """Компактный текстовый профиль проекта."""
    parts = []
    if project["name"]:
        parts.append(f"Название: {project['name']}")
    if project["description"]:
        parts.append(f"Описание: {project['description']}")
    if project["audience"]:
        parts.append(f"Аудитория: {project['audience']}")
    if project["tone"]:
        parts.append(f"Тон: {project['tone']}")
    if project["goals"]:
        parts.append(f"Цели: {project['goals']}")
    if project["cta"]:
        parts.append(f"CTA: {project['cta']}")
    if project["themes"]:
        parts.append(f"Темы: {project['themes']}")
    if project["forbidden"]:
        parts.append(f"Запрещено: {project['forbidden']}")
    return "\n".join(parts) if parts else "Профиль не задан."


# ─── Основная функция ─────────────────────────────────────────────────────────


def generate_plan_filtered(
    project_id: int,
    platform: str,
    date_from: str,
    date_to: str,
    only_content_type: str | None = None,
) -> int:
    """
    Генерирует контент-план с опциональной фильтрацией по типу контента.

    При only_content_type != None в промпт передаётся только этот тип, и
    вставляются только items с данным content_type.

    Args:
        project_id:        ID проекта в БД.
        platform:          Ключ платформы.
        date_from:         Начало периода YYYY-MM-DD.
        date_to:           Конец периода YYYY-MM-DD.
        only_content_type: Если задан — генерировать только для этого типа.

    Returns:
        Число созданных пунктов плана.

    Raises:
        LLMError: при ошибке LLM.
    """
    with get_db() as db:
        project = db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            logger.warning("generate_plan_filtered: project_id=%d не найден", project_id)
            return 0

        strategy_row = _get_active_strategy(db, project_id)
        if strategy_row is None:
            logger.warning(
                "generate_plan_filtered: project_id=%d — нет активной стратегии", project_id
            )
            return 0

        try:
            strategy_data = json.loads(strategy_row["strategy"]) if strategy_row["strategy"] else {}
        except (json.JSONDecodeError, TypeError):
            strategy_data = {}
        platforms_section = strategy_data.get("platforms", {})
        platform_strategy = platforms_section.get(platform)
        if platform_strategy is None:
            logger.warning(
                "generate_plan_filtered: project_id=%d, platform=%s — нет секции в стратегии",
                project_id, platform,
            )
            return 0

        enabled_types = _get_enabled_types(db, project_id, platform)

        # При точечной перегенерации — оставить только запрошенный тип
        if only_content_type is not None:
            if only_content_type not in enabled_types or not enabled_types[only_content_type]:
                logger.warning(
                    "generate_plan_filtered: content_type=%r не включён для %s",
                    only_content_type, platform,
                )
                return 0
            # Фильтруем enabled_types до одного
            enabled_types = {only_content_type: True}

        active_types = {t for t, on in enabled_types.items() if on}
        if not active_types:
            logger.warning(
                "generate_plan_filtered: project_id=%d, platform=%s — нет включённых типов",
                project_id, platform,
            )
            return 0

        learnings_text = _get_learnings(db, project_id)
        used_topics_text = _get_used_topics(db, project_id, platform)
        strategy_id = strategy_row["id"]

    profile_text = _profile_block(project)
    platform_strategy_text = json.dumps(platform_strategy, ensure_ascii=False, indent=2)
    content_types_text = _content_types_block(platform, enabled_types)

    prompt = load_prompt(
        "plan",
        PROFILE=profile_text,
        PLATFORM=platform,
        PLATFORM_STRATEGY=platform_strategy_text,
        CONTENT_TYPES=content_types_text,
        DATE_FROM=date_from,
        DATE_TO=date_to,
        LEARNINGS=learnings_text,
        USED_TOPICS=used_topics_text,
    )
    messages = [{"role": "user", "content": prompt}]

    raw = chat(messages, purpose="plan", json_mode=True)
    try:
        items = _parse_plan_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("generate_plan_filtered: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка парсинга JSON: {exc}. Ответь ТОЛЬКО валидным JSON-объектом "
                    'вида {"items": [...]} без пояснений и без markdown-фенсов.'
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="plan", json_mode=True)
        items = _parse_plan_json(raw2)

    try:
        df = _date.fromisoformat(date_from)
        dt = _date.fromisoformat(date_to)
    except ValueError:
        logger.error(
            "generate_plan_filtered: неверный формат дат: %s — %s", date_from, date_to
        )
        return 0

    inserted = 0
    with get_db() as db:
        for item in items:
            raw_date = (item.get("date") or "").strip()
            try:
                item_date = _date.fromisoformat(raw_date)
            except ValueError:
                continue
            if item_date < df or item_date > dt:
                continue

            ctype = (item.get("content_type") or "").strip()
            if not catalog.is_valid(platform, ctype):
                continue
            if ctype not in active_types:
                continue

            title = (item.get("title") or "").strip()
            if not title:
                continue

            time_slot = (item.get("time") or "12:00").strip()
            if not re.match(r"^\d{2}:\d{2}$", time_slot):
                time_slot = "12:00"

            brief = item.get("brief")
            brief_json = json.dumps(brief, ensure_ascii=False) if isinstance(brief, dict) else None

            existing = db.execute(
                "SELECT id FROM plan_items "
                "WHERE project_id=? AND platform=? AND content_type=? AND date=? AND title=?",
                (project_id, platform, ctype, raw_date, title),
            ).fetchone()
            if existing is not None:
                continue

            db.execute(
                """
                INSERT INTO plan_items
                    (project_id, strategy_id, platform, content_type, date, time_slot,
                     title, brief, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed')
                """,
                (project_id, strategy_id, platform, ctype, raw_date, time_slot, title, brief_json),
            )
            inserted += 1

    logger.info(
        "generate_plan_filtered: project_id=%d, platform=%s, type=%s, %s–%s → %d пунктов",
        project_id, platform, only_content_type or "all", date_from, date_to, inserted,
    )
    return inserted


def generate_plan(
    project_id: int,
    platform: str,
    date_from: str,
    date_to: str,
) -> int:
    """
    Генерирует контент-план для проекта на заданной платформе и период.

    Args:
        project_id: ID проекта в БД.
        platform:   Ключ платформы (telegram, vk, youtube, instagram, dzen).
        date_from:  Начало периода YYYY-MM-DD (включительно).
        date_to:    Конец периода YYYY-MM-DD (включительно).

    Returns:
        Число созданных пунктов плана (0 если нет стратегии/платформы/типов).

    Raises:
        LLMError: при ошибке LLM после retry (пробрасывается — ловит brain.py).
    """
    with get_db() as db:
        # Загружаем проект
        project = db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project is None:
            logger.warning("generate_plan: project_id=%d не найден", project_id)
            return 0

        # Активная стратегия
        strategy_row = _get_active_strategy(db, project_id)
        if strategy_row is None:
            logger.warning(
                "generate_plan: project_id=%d — нет активной стратегии", project_id
            )
            return 0

        # Секция платформы в стратегии
        try:
            strategy_data = json.loads(strategy_row["strategy"]) if strategy_row["strategy"] else {}
        except (json.JSONDecodeError, TypeError):
            strategy_data = {}
        platforms_section = strategy_data.get("platforms", {})
        platform_strategy = platforms_section.get(platform)
        if platform_strategy is None:
            logger.warning(
                "generate_plan: project_id=%d, platform=%s — нет секции в стратегии",
                project_id, platform,
            )
            return 0

        # Включённые типы
        enabled_types = _get_enabled_types(db, project_id, platform)
        active_types = {t for t, on in enabled_types.items() if on}
        if not active_types:
            logger.warning(
                "generate_plan: project_id=%d, platform=%s — нет включённых типов",
                project_id, platform,
            )
            return 0

        # Learnings и использованные темы
        learnings_text = _get_learnings(db, project_id)
        used_topics_text = _get_used_topics(db, project_id, platform)

        strategy_id = strategy_row["id"]

    # Строим промпт
    profile_text = _profile_block(project)
    platform_strategy_text = json.dumps(platform_strategy, ensure_ascii=False, indent=2)
    content_types_text = _content_types_block(platform, enabled_types)

    prompt = load_prompt(
        "plan",
        PROFILE=profile_text,
        PLATFORM=platform,
        PLATFORM_STRATEGY=platform_strategy_text,
        CONTENT_TYPES=content_types_text,
        DATE_FROM=date_from,
        DATE_TO=date_to,
        LEARNINGS=learnings_text,
        USED_TOPICS=used_topics_text,
    )
    messages = [{"role": "user", "content": prompt}]

    # LLM-вызов + retry
    raw = chat(messages, purpose="plan", json_mode=True)
    try:
        items = _parse_plan_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("generate_plan: первая попытка парсинга провалилась (%s), retry", exc)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    f"Ошибка парсинга JSON: {exc}. Ответь ТОЛЬКО валидным JSON-объектом "
                    'вида {"items": [...]} без пояснений и без markdown-фенсов.'
                ),
            },
        ]
        raw2 = chat(retry_messages, purpose="plan", json_mode=True)
        items = _parse_plan_json(raw2)
        # LLMError пробрасывается наружу если retry тоже невалидный

    # Вставляем в БД с валидацией и дедупликацией
    try:
        df = _date.fromisoformat(date_from)
        dt = _date.fromisoformat(date_to)
    except ValueError:
        logger.error(
            "generate_plan: неверный формат дат: %s — %s", date_from, date_to
        )
        return 0

    inserted = 0
    with get_db() as db:
        for item in items:
            # Валидация даты
            raw_date = (item.get("date") or "").strip()
            try:
                item_date = _date.fromisoformat(raw_date)
            except ValueError:
                logger.debug("generate_plan: пропуск item с неверной датой %r", raw_date)
                continue
            if item_date < df or item_date > dt:
                logger.debug(
                    "generate_plan: пропуск item дата %s вне периода [%s, %s]",
                    raw_date, date_from, date_to,
                )
                continue

            # Валидация content_type
            ctype = (item.get("content_type") or "").strip()
            if not catalog.is_valid(platform, ctype):
                logger.debug(
                    "generate_plan: пропуск item content_type=%r невалиден для %s",
                    ctype, platform,
                )
                continue
            if ctype not in active_types:
                logger.debug(
                    "generate_plan: пропуск item content_type=%r не включён для %s",
                    ctype, platform,
                )
                continue

            # Валидация title
            title = (item.get("title") or "").strip()
            if not title:
                logger.debug("generate_plan: пропуск item с пустым title")
                continue

            # Время по умолчанию
            time_slot = (item.get("time") or "12:00").strip()
            if not re.match(r"^\d{2}:\d{2}$", time_slot):
                time_slot = "12:00"

            # Brief
            brief = item.get("brief")
            brief_json = json.dumps(brief, ensure_ascii=False) if isinstance(brief, dict) else None

            # Дедупликация: не вставлять если уже есть такой же
            existing = db.execute(
                "SELECT id FROM plan_items "
                "WHERE project_id=? AND platform=? AND content_type=? AND date=? AND title=?",
                (project_id, platform, ctype, raw_date, title),
            ).fetchone()
            if existing is not None:
                logger.debug(
                    "generate_plan: дедуп — пропуск duplicate %s %s %s «%s»",
                    raw_date, platform, ctype, title,
                )
                continue

            db.execute(
                """
                INSERT INTO plan_items
                    (project_id, strategy_id, platform, content_type, date, time_slot,
                     title, brief, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'proposed')
                """,
                (project_id, strategy_id, platform, ctype, raw_date, time_slot, title, brief_json),
            )
            inserted += 1

    logger.info(
        "generate_plan: project_id=%d, platform=%s, %s–%s → %d пунктов",
        project_id, platform, date_from, date_to, inserted,
    )
    return inserted
