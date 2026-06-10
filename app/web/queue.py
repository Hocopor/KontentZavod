"""
Шахматка готового контента (этап 7, волна D).

Роуты:
    GET  /queue                              — шахматка сгенерированного контента
    GET  /queue/items/{plan_item_id}/modal   — HTMX-модалка превью
    POST /queue/items/{plan_item_id}/cancel  — снять с публикации (только planned)
    POST /queue/items/{plan_item_id}/retry   — повторить генерацию (только error)
"""
import calendar
import json
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import catalog
from app.db import get_db
from app.templates_env import templates

router = APIRouter(prefix="/queue")

# Русские названия месяцев
_MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Метки статусов план-ячеек
_STATUS_LABELS = {
    "generating": "Генерируется",
    "generated":  "Сгенерирован",
    "error":      "Ошибка",
}

# Индикаторы статусов ячеек
_STATUS_ICONS = {
    "generating": "⏳",
    "error":      "⚠",
}

# Индикаторы статусов публикации (schedule)
_SCHEDULE_ICONS = {
    "planned":        "📅",
    "published":      "🚀",
    "manual_pending": "✋",
    "error":          "❌",
}

# Цвета статусов
_STATUS_COLORS = {
    "generating": "#38bdf8",
    "generated":  "#6c63ff",
    "error":      "#ef4444",
}

# Метки платформ
_PLATFORM_LABELS = {
    "telegram":  "Telegram",
    "vk":        "ВКонтакте",
    "youtube":   "YouTube",
    "instagram": "Instagram",
    "dzen":      "Яндекс.Дзен",
}

# Статусы которые нельзя отменить
_CANCEL_FORBIDDEN = {"published", "manual_pending", "manual_done", "publishing"}


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _get_projects_list(db) -> list[dict]:
    """Вернуть активные проекты."""
    rows = db.execute(
        "SELECT id, slug, name, stage, settings FROM projects WHERE status='active' ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_project_by_slug(db, slug: str):
    """Вернуть строку projects или None."""
    return db.execute(
        "SELECT * FROM projects WHERE slug=? AND status='active'", (slug,)
    ).fetchone()


def _get_schedule_for_content(db, content_id: int) -> dict | None:
    """Вернуть первую schedule-запись для content_id или None."""
    row = db.execute(
        "SELECT id, status, published_url, error_text FROM schedule WHERE content_id=? LIMIT 1",
        (content_id,),
    ).fetchone()
    return dict(row) if row else None


def _build_board_context(db, project: dict, year: int, month: int) -> dict:
    """
    Собрать контекст для шахматки готового контента:
    - статусы: generating, generated, error
    - строки: платформа → тип
    - колонки: дни месяца
    - в ячейке: индикатор по статусу
    """
    today = date.today()
    days_in_month = calendar.monthrange(year, month)[1]
    days = list(range(1, days_in_month + 1))

    weekday_abbrs = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    first_weekday = calendar.weekday(year, month, 1)
    day_headers = []
    for d in days:
        wd = (first_weekday + d - 1) % 7
        is_today = (year == today.year and month == today.month and d == today.day)
        day_headers.append({"day": d, "wd": weekday_abbrs[wd], "today": is_today})

    # Загрузить plan_items со статусами generating/generated/error за месяц
    month_str = f"{year:04d}-{month:02d}"
    rows = db.execute(
        """
        SELECT * FROM plan_items
        WHERE project_id=? AND date LIKE ?
          AND status IN ('generating', 'generated', 'error')
        ORDER BY date, time_slot NULLS LAST, id
        """,
        (project["id"], f"{month_str}-%"),
    ).fetchall()

    items_by_key: dict[tuple, list] = {}  # (platform, content_type, day) -> list[dict]
    platform_types: dict[str, set] = {}   # platform -> set of content_types

    for row in rows:
        item = dict(row)
        item["status_label"] = _STATUS_LABELS.get(item["status"], item["status"])
        item["status_color"] = _STATUS_COLORS.get(item["status"], "#888")

        ti = catalog.type_info(item["platform"], item["content_type"])
        item["type_label"] = ti["label"] if ti else item["content_type"]

        # Индикатор ячейки
        if item["status"] in ("generating", "error"):
            item["cell_icon"] = _STATUS_ICONS[item["status"]]
        else:
            # generated: смотрим schedule
            sched = None
            if item["content_id"]:
                sched = _get_schedule_for_content(db, item["content_id"])
            if sched:
                item["cell_icon"] = _SCHEDULE_ICONS.get(sched["status"], "✓")
            else:
                item["cell_icon"] = "✓"

        # День из даты
        day_part = item["date"][8:10]
        try:
            day_int = int(day_part)
        except ValueError:
            continue

        key = (item["platform"], item["content_type"], day_int)
        items_by_key.setdefault(key, []).append(item)
        platform_types.setdefault(item["platform"], set()).add(item["content_type"])

    # Упорядоченные строки шахматки
    platform_order = ["telegram", "vk", "youtube", "instagram", "dzen"]
    rows_data = []
    for plat in platform_order:
        if plat not in platform_types:
            continue
        allowed_order = list(catalog.allowed_types(plat).keys())
        types_sorted = [t for t in allowed_order if t in platform_types[plat]]
        type_rows = []
        for ctype in types_sorted:
            ti = catalog.type_info(plat, ctype)
            cells = []
            for d in days:
                key = (plat, ctype, d)
                cells.append(items_by_key.get(key, []))
            type_rows.append({
                "content_type": ctype,
                "type_label": ti["label"] if ti else ctype,
                "cells": cells,
            })
        rows_data.append({
            "platform": plat,
            "platform_label": _PLATFORM_LABELS.get(plat, plat),
            "type_rows": type_rows,
        })

    # Навигация по месяцам
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    prev_month_str = f"{prev_year:04d}-{prev_month:02d}"
    next_month_str = f"{next_year:04d}-{next_month:02d}"

    return {
        "project": dict(project),
        "year": year,
        "month": month,
        "month_name": _MONTH_NAMES[month],
        "day_headers": day_headers,
        "rows_data": rows_data,
        "prev_month_str": prev_month_str,
        "next_month_str": next_month_str,
        "today": today,
        "status_labels": _STATUS_LABELS,
        "status_colors": _STATUS_COLORS,
    }


def _get_item_or_404(db, item_id: int):
    """Вернуть plan_item или None."""
    return db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()


# ─── Роуты ────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def queue_board(
    request: Request,
    project: str = "",
    month: str = "",
):
    """
    Главная страница шахматки готового контента.
    ?project=slug&month=YYYY-MM
    """
    today = date.today()

    # Разобрать month=YYYY-MM
    year = today.year
    month_int = today.month
    if month:
        try:
            parts = month.split("-")
            year = int(parts[0])
            month_int = int(parts[1])
            year = max(2020, min(2099, year))
            month_int = max(1, min(12, month_int))
        except (ValueError, IndexError):
            year = today.year
            month_int = today.month

    with get_db() as db:
        all_projects = _get_projects_list(db)

        # Если передан конкретный slug — проверить сразу, независимо от списка
        if project:
            proj_row = _get_project_by_slug(db, project)
            if proj_row is None:
                return HTMLResponse("Проект не найден", status_code=404)
        else:
            proj_row = None

        # Пустое состояние: нет проектов
        if not all_projects:
            return templates.TemplateResponse(request, "queue/index.html", {
                "all_projects": [],
                "project": None,
                "year": year,
                "month": month_int,
                "month_name": _MONTH_NAMES[month_int],
                "rows_data": [],
                "day_headers": [],
                "prev_month_str": f"{year:04d}-{(month_int - 1) if month_int > 1 else 12:02d}",
                "next_month_str": f"{year:04d}-{(month_int + 1) if month_int < 12 else 1:02d}",
                "today": today,
                "status_labels": _STATUS_LABELS,
                "status_colors": _STATUS_COLORS,
            })

        # Если slug не передан — выбрать проект по умолчанию (running или первый)
        if proj_row is None:
            for p in all_projects:
                if p["stage"] == "running":
                    proj_row = db.execute("SELECT * FROM projects WHERE id=?", (p["id"],)).fetchone()
                    break
            if proj_row is None:
                proj_row = db.execute("SELECT * FROM projects WHERE id=?", (all_projects[0]["id"],)).fetchone()

        ctx = _build_board_context(db, dict(proj_row), year, month_int)
        ctx["all_projects"] = all_projects

    return templates.TemplateResponse(request, "queue/index.html", ctx)


@router.get("/items/{plan_item_id}/modal", response_class=HTMLResponse)
async def queue_item_modal(request: Request, plan_item_id: int):
    """HTMX-модалка превью контента."""
    with get_db() as db:
        row = _get_item_or_404(db, plan_item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        item = dict(row)
        item["status_label"] = _STATUS_LABELS.get(item["status"], item["status"])
        item["status_color"] = _STATUS_COLORS.get(item["status"], "#888")

        ti = catalog.type_info(item["platform"], item["content_type"])
        item["type_label"] = ti["label"] if ti else item["content_type"]
        item["platform_label"] = _PLATFORM_LABELS.get(item["platform"], item["platform"])

        # Контент
        content = None
        schedule = None
        if item["content_id"]:
            crow = db.execute("SELECT * FROM content WHERE id=?", (item["content_id"],)).fetchone()
            if crow:
                content = dict(crow)
                # Распарсить texts и files JSON
                try:
                    content["texts_parsed"] = json.loads(content["texts"]) if content["texts"] else {}
                except (json.JSONDecodeError, TypeError):
                    content["texts_parsed"] = {}
                try:
                    content["files_parsed"] = json.loads(content["files"]) if content["files"] else {}
                except (json.JSONDecodeError, TypeError):
                    content["files_parsed"] = {}

                # schedule
                srow = db.execute(
                    "SELECT * FROM schedule WHERE content_id=? LIMIT 1",
                    (item["content_id"],),
                ).fetchone()
                if srow:
                    schedule = dict(srow)

    return templates.TemplateResponse(request, "queue/_modal.html", {
        "item": item,
        "content": content,
        "schedule": schedule,
    })


@router.post("/items/{plan_item_id}/cancel", response_class=HTMLResponse)
async def queue_item_cancel(plan_item_id: int):
    """
    Снять с публикации: найти schedule по content_id,
    если status='planned' → DELETE. Иначе — 422.
    """
    with get_db() as db:
        row = _get_item_or_404(db, plan_item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        item = dict(row)
        if not item["content_id"]:
            return HTMLResponse(
                "Нет связанного контента для отмены публикации",
                status_code=422,
            )

        srow = db.execute(
            "SELECT id, status FROM schedule WHERE content_id=? LIMIT 1",
            (item["content_id"],),
        ).fetchone()

        if srow is None:
            return HTMLResponse("Нет записи в расписании", status_code=422)

        if srow["status"] in _CANCEL_FORBIDDEN:
            return HTMLResponse(
                f"Нельзя отменить публикацию со статусом «{srow['status']}»",
                status_code=422,
            )

        if srow["status"] != "planned":
            return HTMLResponse(
                f"Нельзя отменить публикацию со статусом «{srow['status']}»",
                status_code=422,
            )

        db.execute("DELETE FROM schedule WHERE id=?", (srow["id"],))

    return HTMLResponse("Публикация отменена", status_code=200)


@router.post("/items/{plan_item_id}/retry", response_class=HTMLResponse)
async def queue_item_retry(plan_item_id: int):
    """
    Повторить генерацию: plan_item status='error' → 'approved',
    error_text=NULL, content_id=NULL. Для других статусов — 422.
    """
    with get_db() as db:
        row = _get_item_or_404(db, plan_item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        if row["status"] != "error":
            return HTMLResponse(
                f"Повторить можно только пункт со статусом error, текущий: {row['status']}",
                status_code=422,
            )

        db.execute(
            """
            UPDATE plan_items
            SET status='approved', error_text=NULL, content_id=NULL,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (plan_item_id,),
        )

    return HTMLResponse("Поставлено на повторную генерацию", status_code=200)
