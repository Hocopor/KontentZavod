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
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date

from app import catalog
from app.config import settings
from app.db import get_db, get_project_settings
from app.llm import chat, LLMError
from app.pipeline.prompts import load_prompt, load_rules
from app.pipeline.slots import build_slots, week_index, _resolve_anchor
from app.services.cleanup import delete_content_cascade

# Русские названия дней недели (для слотов в промпте)
_WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
                "пятница", "суббота", "воскресенье"]

# Плейсхолдер для слотов, по которым LLM не вернул тему
_PLACEHOLDER_TITLE = "(тема не сгенерирована — нажмите 🔄)"

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


def _rubrics_block(platform_strategy: dict | None) -> str:
    """
    Рубрики платформы списком «• name [goal] — description».
    Поддерживает легаси-строки (старый формат rubrics: ["Рубрика 1", ...]).
    """
    if not isinstance(platform_strategy, dict):
        return "Рубрики не заданы."
    rubrics = platform_strategy.get("rubrics")
    if not isinstance(rubrics, list) or not rubrics:
        return "Рубрики не заданы."
    lines: list[str] = []
    for r in rubrics:
        if isinstance(r, dict):
            name = (r.get("name") or "").strip()
            if not name:
                continue
            goal = (r.get("goal") or "").strip()
            desc = (r.get("description") or "").strip()
            line = f"• {name}"
            if goal:
                line += f" [{goal}]"
            if desc:
                line += f" — {desc}"
            lines.append(line)
        elif isinstance(r, str) and r.strip():
            lines.append(f"• {r.strip()}")
    return "\n".join(lines) if lines else "Рубрики не заданы."


def _directives_block(db, project_id: int) -> str:
    """Активные директивы проекта компактным текстом («• текст [scope]»)."""
    rows = db.execute(
        "SELECT text, scope FROM directives "
        "WHERE project_id=? AND status='active' ORDER BY id",
        (project_id,),
    ).fetchall()
    if not rows:
        return "Нет."
    return "\n".join(f"• {(r['text'] or '').strip()} [{r['scope']}]" for r in rows)


def _slot_for_prompt(slot_id: int, slot: dict, anchor: _date) -> dict:
    """Слот в формате для LLM-промпта (с русским днём недели)."""
    try:
        d = _date.fromisoformat(slot["date"])
        weekday = _WEEKDAYS_RU[d.weekday()]
    except (ValueError, KeyError):
        weekday = ""
    return {
        "slot_id": slot_id,
        "date": slot["date"],
        "weekday": weekday,
        "content_type": slot["content_type"],
        "goal": slot.get("goal", ""),
    }


# ─── Основная функция ─────────────────────────────────────────────────────────


def _fill_week_group(
    project_id: int,
    platform: str,
    platform_strategy: dict | None,
    project: dict,
    id_to_slot: dict,
    anchor: _date,
) -> dict:
    """
    Полный цикл одной недели: prompt → chat(+retry) → retry недостающих слотов.
    Своё db-соединение на каждый шаг (поток-безопасно).
    Возвращает {slot_id: item|None}.
    """
    # 1) первичный промпт + chat
    with get_db() as db:
        prompt_slots = [_slot_for_prompt(sid, id_to_slot[sid], anchor) for sid in sorted(id_to_slot)]
        messages = _build_prompt_for_group(db, project_id, platform, platform_strategy, project, prompt_slots)
    try:
        by_id = _chat_plan_with_retry(messages)
    except LLMError as exc:
        logger.warning(
            "generate_plan: LLMError в группе недели (project=%d platform=%s): %s — пропуск",
            project_id, platform, exc,
        )
        return {sid: None for sid in id_to_slot}

    # 2) джойн + сбор недостающих
    results: dict = {}
    missing_ids: list = []
    for sid in sorted(id_to_slot):
        item = by_id.get(sid)
        title = (item.get("title") or "").strip() if isinstance(item, dict) else ""
        if isinstance(item, dict) and title:
            results[sid] = item
        else:
            missing_ids.append(sid)

    # 3) один retry по недостающим
    if missing_ids:
        with get_db() as db:
            retry_prompt_slots = [_slot_for_prompt(sid, id_to_slot[sid], anchor) for sid in missing_ids]
            retry_messages = _build_prompt_for_group(
                db, project_id, platform, platform_strategy, project, retry_prompt_slots,
                prefix="Это повторный запрос — предыдущий ответ пропустил эти слоты.",
            )
        try:
            retry_by_id = _chat_plan_with_retry(retry_messages)
        except LLMError as exc:
            logger.warning("generate_plan: LLMError в retry недели: %s", exc)
            retry_by_id = {}
        for sid in missing_ids:
            item = retry_by_id.get(sid)
            title = (item.get("title") or "").strip() if isinstance(item, dict) else ""
            results[sid] = item if (isinstance(item, dict) and title) else None

    return results


def _build_prompt_for_group(
    db,
    project_id: int,
    platform: str,
    platform_strategy: dict | None,
    project,
    prompt_slots: list[dict],
    prefix: str = "",
) -> list[dict]:
    """Собрать messages для одной группы слотов (одна неделя)."""
    rubrics_text = _rubrics_block(platform_strategy)
    directives_text = _directives_block(db, project_id)
    settings = get_project_settings(project["settings"])
    rules_text = load_rules(settings)
    learnings_text = _get_learnings(db, project_id)
    used_topics_text = _get_used_topics(db, project_id, platform)
    profile_text = _profile_block(project)

    slots_json = json.dumps(prompt_slots, ensure_ascii=False, indent=2)
    if prefix:
        slots_json = prefix + "\n\n" + slots_json

    prompt = load_prompt(
        "plan",
        RULES=rules_text,
        DIRECTIVES=directives_text,
        PROFILE=profile_text,
        PLATFORM=platform,
        RUBRICS=rubrics_text,
        LEARNINGS=learnings_text,
        USED_TOPICS=used_topics_text,
        SLOTS=slots_json,
    )
    return [{"role": "user", "content": prompt}]


def _chat_plan_with_retry(messages: list[dict]) -> dict[int, dict]:
    """
    Один LLM-вызов плана + 1 retry при невалидном JSON.
    Возвращает {slot_id: item} по ответу модели.
    """
    raw = chat(messages, purpose="plan", json_mode=True)
    try:
        items = _parse_plan_json(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("plan: первая попытка парсинга провалилась (%s), retry", exc)
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

    by_id: dict[int, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("slot_id")
        try:
            sid = int(sid)
        except (TypeError, ValueError):
            continue
        by_id[sid] = it
    return by_id


def _generate_plan_impl(
    project_id: int,
    platform: str,
    date_from: str,
    date_to: str,
    only_content_type: str | None = None,
) -> int:
    """
    Планнер v2: количество слотов задаёт КОД (build_slots по стратегии),
    LLM наполняет каждый слот темой/брифом. Понедельный цикл по якорю фаз.

    Args:
        project_id:        ID проекта.
        platform:          ключ платформы.
        date_from:         начало периода YYYY-MM-DD.
        date_to:           конец периода YYYY-MM-DD.
        only_content_type: если задан — только этот тип.

    Returns:
        Число вставленных plan_items.

    Raises:
        LLMError: ошибка LLM конкретной группы логируется и не прерывает
                  остальные группы (вставленное не откатывается).
    """
    with get_db() as db:
        project_row = db.execute(
            "SELECT * FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        if project_row is None:
            logger.warning("generate_plan: project_id=%d не найден", project_id)
            return 0
        # Отвязываем project от соединения — читаем как dict для потоко-безопасного доступа в потоках
        project = dict(project_row)

        strategy_row = _get_active_strategy(db, project_id)
        if strategy_row is None:
            logger.warning("generate_plan: project_id=%d — нет активной стратегии", project_id)
            return 0

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

        strategy_id = strategy_row["id"]

        # Якорь фаз — для группировки слотов по неделям (тот же расчёт, что в slots.py)
        created_at = None
        try:
            created_at = strategy_row["created_at"]
        except (KeyError, IndexError):
            created_at = None
        anchor = _resolve_anchor(strategy_data, created_at)

        # Слоты (детерминированно)
        slots = build_slots(db, project_id, platform, date_from, date_to, only_content_type)

    if not slots:
        return 0

    # Группируем слоты по неделям якоря; slot_id с 1 ВНУТРИ группы
    groups: dict[int, list[dict]] = {}
    for slot in slots:
        try:
            d = _date.fromisoformat(slot["date"])
        except ValueError:
            continue
        k = week_index(anchor, d)
        groups.setdefault(k, []).append(slot)

    # Подготовка карт слотов по неделям (slot_id с 1 ВНУТРИ недели), детерминированно
    week_maps: dict[int, dict] = {}
    for k in sorted(groups.keys()):
        group = groups[k]
        group.sort(key=lambda s: (s["date"], s["time_slot"]))
        week_maps[k] = {i + 1: group[i] for i in range(len(group))}

    # Наполнение недель: параллельно (если concurrency>1 и недель >1), иначе последовательно
    concurrency = max(1, int(settings.PLAN_LLM_CONCURRENCY))
    results_by_k: dict[int, dict] = {}
    if concurrency == 1 or len(week_maps) <= 1:
        for k in sorted(week_maps):
            results_by_k[k] = _fill_week_group(
                project_id, platform, platform_strategy, project, week_maps[k], anchor
            )
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as ex:
            futs = {
                k: ex.submit(
                    _fill_week_group,
                    project_id, platform, platform_strategy, project, week_maps[k], anchor,
                )
                for k in week_maps
            }
            for k, fut in futs.items():
                try:
                    results_by_k[k] = fut.result()
                except Exception:
                    logger.exception("generate_plan: ошибка недели k=%s", k)
                    results_by_k[k] = {sid: None for sid in week_maps[k]}

    # Сборка filled В ТОМ ЖЕ ПОРЯДКЕ, что и раньше (по неделям, внутри — по slot_id)
    filled: list[tuple[dict, dict | None]] = []
    for k in sorted(week_maps):
        id_to_slot = week_maps[k]
        res = results_by_k.get(k, {})
        for sid in sorted(id_to_slot):
            filled.append((id_to_slot[sid], res.get(sid)))

    if not filled:
        return 0

    # Вставка plan_items: date/time_slot/content_type/goal — ИЗ СЛОТА, не из LLM
    inserted = 0
    with get_db() as db:
        for slot, item in filled:
            ctype = slot["content_type"]
            slot_date = slot["date"]
            time_slot = slot["time_slot"]
            goal = slot.get("goal")

            if isinstance(item, dict):
                title = (item.get("title") or "").strip()
                brief = item.get("brief")
                brief_json = (
                    json.dumps(brief, ensure_ascii=False) if isinstance(brief, dict) else None
                )
            else:
                title = ""
                brief_json = None
            if not title:
                title = _PLACEHOLDER_TITLE
                brief_json = None

            # Страховочный дедуп по (project, platform, content_type, date, time_slot)
            existing = db.execute(
                "SELECT id FROM plan_items "
                "WHERE project_id=? AND platform=? AND content_type=? AND date=? AND time_slot=? "
                "AND status != 'rejected'",
                (project_id, platform, ctype, slot_date, time_slot),
            ).fetchone()
            if existing is not None:
                continue

            db.execute(
                """
                INSERT INTO plan_items
                    (project_id, strategy_id, platform, content_type, date, time_slot,
                     title, brief, goal, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed')
                """,
                (project_id, strategy_id, platform, ctype, slot_date, time_slot,
                 title, brief_json, goal),
            )
            inserted += 1

    logger.info(
        "generate_plan: project_id=%d, platform=%s, type=%s, %s–%s → %d пунктов",
        project_id, platform, only_content_type or "all", date_from, date_to, inserted,
    )
    return inserted


def generate_plan_filtered(
    project_id: int,
    platform: str,
    date_from: str,
    date_to: str,
    only_content_type: str | None = None,
) -> int:
    """
    Генерирует контент-план v2 с опциональной фильтрацией по типу контента.

    Количество публикаций задаёт КОД (build_slots по активной стратегии),
    LLM лишь наполняет слоты темами. При only_content_type генерируются
    только слоты этого типа.

    Returns:
        Число созданных пунктов плана.

    Raises:
        LLMError: пробрасывается, если возникла вне обработки группы.
    """
    return _generate_plan_impl(
        project_id, platform, date_from, date_to, only_content_type
    )


def generate_plan(
    project_id: int,
    platform: str,
    date_from: str,
    date_to: str,
) -> int:
    """
    Генерирует контент-план v2 для проекта на платформе и периоде.

    Тонкая обёртка над generate_plan_filtered без фильтра по типу.

    Returns:
        Число созданных пунктов плана (0 если нет стратегии/слотов).

    Raises:
        LLMError: пробрасывается, если возникла вне обработки группы.
    """
    return _generate_plan_impl(project_id, platform, date_from, date_to, None)


