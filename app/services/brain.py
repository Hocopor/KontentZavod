"""
Фоновый «мозг» завода (этап 7, волна C).

process_brain() — джоб, вызываемый раз в 2 минуты (подключает оркестратор
в scheduler.py — сам файл НЕ трогаем).

Логика:
1. Стратегии: берём ОДНУ запись со status='generating' (старейшую по created_at)
   → вызываем build_strategy(id). Если нашли и обработали — выходим (тик короткий).
2. Планы: перебираем running-проекты с активной стратегией и plan_paused==0.
   Для каждого проекта и каждой включённой платформы:
   - Считаем MAX(date) из plan_items (кроме rejected) — текущее покрытие.
   - Цель покрытия = today + plan_horizon_days.
   - Если покрытие < цели → generate_plan(...) и немедленно выходим
     (одна платформа за тик, остальное догенерируется следующими тиками).
3. Все исключения подавляются через try/except — джоб не должен падать.
"""
import json
import logging
from datetime import date, timedelta

from app.config import settings
from app.db import get_db, get_project_settings
from app.llm import LLMError

logger = logging.getLogger(__name__)


def process_brain() -> None:
    """
    Один тик фонового мозга: стратегии → rolling-план.

    Никаких исключений наружу: всё оборачивается в try/except.
    """
    # ── 1. Обработка generating-стратегий ─────────────────────────────────────
    try:
        strategy_id = _pick_generating_strategy()
        if strategy_id is not None:
            logger.info("process_brain: обрабатываем strategy_id=%d", strategy_id)
            from app.pipeline.strategy import build_strategy
            build_strategy(strategy_id)
            # build_strategy сам ловит исключения и пишет status='error'
            return  # тик обработан
    except Exception:
        logger.exception("process_brain: ошибка при обработке стратегии")
        return

    # ── 2. Rolling-покрытие контент-плана ──────────────────────────────────────
    try:
        _fill_plan()
    except Exception:
        logger.exception("process_brain: ошибка при заполнении плана")


# ─── Внутренние функции ───────────────────────────────────────────────────────


def _pick_generating_strategy() -> int | None:
    """
    Вернуть id старейшей стратегии со status='generating', или None.
    """
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM strategies WHERE status='generating' "
            "ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    return row["id"] if row else None


def _get_enabled_platforms(db, project_id: int) -> list[str]:
    """
    Вернуть список включённых платформ проекта.
    """
    rows = db.execute(
        "SELECT platform FROM project_platforms WHERE project_id=? AND enabled=1",
        (project_id,),
    ).fetchall()
    return [r["platform"] for r in rows]


def _plan_coverage(db, project_id: int, platform: str) -> date | None:
    """
    MAX(date) из plan_items этого проекта+платформы (кроме rejected).
    Возвращает date или None если плана ещё нет.
    """
    row = db.execute(
        "SELECT MAX(date) AS max_date FROM plan_items "
        "WHERE project_id=? AND platform=? AND status != 'rejected'",
        (project_id, platform),
    ).fetchone()
    max_date_str = row["max_date"] if row else None
    if not max_date_str:
        return None
    try:
        return date.fromisoformat(max_date_str)
    except ValueError:
        return None


def _has_active_strategy(db, project_id: int) -> bool:
    """Есть ли у проекта активная стратегия."""
    row = db.execute(
        "SELECT id FROM strategies WHERE project_id=? AND status='active' LIMIT 1",
        (project_id,),
    ).fetchone()
    return row is not None


def _fill_plan() -> None:
    """
    Для каждого running-проекта с активной стратегией и plan_paused==0:
    проверяем покрытие плана по каждой платформе.
    Если покрытие < today + horizon → generate_plan(...) и выходим из _fill_plan.
    Одна генерация за тик — следующая будет при следующем тике.
    """
    from app.pipeline.planner import generate_plan

    today = date.today()

    # Все running-проекты
    with get_db() as db:
        projects = db.execute(
            "SELECT id, settings FROM projects WHERE stage='running' AND status='active'"
        ).fetchall()

    for proj in projects:
        project_id = proj["id"]
        proj_settings = get_project_settings(proj["settings"])

        # Пропускаем если план на паузе
        if proj_settings.get("plan_paused", 0):
            continue

        horizon_days = proj_settings.get("plan_horizon_days", 30)
        target_date = today + timedelta(days=horizon_days)

        # Проверяем активную стратегию
        with get_db() as db:
            if not _has_active_strategy(db, project_id):
                continue
            platforms = _get_enabled_platforms(db, project_id)

        for platform in platforms:
            try:
                with get_db() as db:
                    coverage = _plan_coverage(db, project_id, platform)

                # Дата начала: максимум(coverage+1, today)
                if coverage is None:
                    date_from = today
                else:
                    date_from = max(coverage + timedelta(days=1), today)

                if date_from > target_date:
                    # Уже покрыто до горизонта
                    continue

                date_from_str = date_from.isoformat()
                tick_max = max(1, int(settings.PLAN_TICK_MAX_DAYS))
                date_to = min(target_date, date_from + timedelta(days=tick_max))
                date_to_str = date_to.isoformat()

                logger.info(
                    "process_brain: generate_plan project_id=%d platform=%s %s–%s",
                    project_id, platform, date_from_str, date_to_str,
                )
                generate_plan(project_id, platform, date_from_str, date_to_str)
                return  # одна платформа за тик

            except LLMError:
                logger.exception(
                    "process_brain: LLMError при generate_plan "
                    "project_id=%d platform=%s",
                    project_id, platform,
                )
                # LLMError не роняет весь процесс — переходим к следующей платформе
            except Exception:
                logger.exception(
                    "process_brain: ошибка при generate_plan "
                    "project_id=%d platform=%s",
                    project_id, platform,
                )
