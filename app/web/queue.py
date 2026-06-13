"""
Шахматка готового контента + ручная публикация + "вне плана" (этап 7, волна D, v2).

Роуты:
    GET  /queue                                  — шахматка + ручная секция + вне плана
    GET  /queue/items/{plan_item_id}/modal        — HTMX-модалка превью
    POST /queue/items/{plan_item_id}/cancel       — снять с публикации (только planned)
    POST /queue/items/{plan_item_id}/regen        — перегенерировать (→ approved, контент/файлы удалены)
    POST /queue/items/{plan_item_id}/delete       — удалить контент + файлы → plan_item rejected
    POST /queue/schedules/{schedule_id}/manual-done  — отметить ручную публикацию
    POST /queue/orphans/{content_id}/delete       — удалить осиротевший контент

Старый POST /retry оставлен для совместимости тестов.
"""
import json
import logging
from datetime import date

import calendar as _calendar_mod

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import catalog
from app.config import settings
from app.db import get_db
from app.publishers.manual import mark_manual_done
from app.services.cleanup import delete_content_files
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/queue")

# ─── Словари ──────────────────────────────────────────────────────────────────

_MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

_STATUS_LABELS: dict[str, str] = {
    # Статусы plan_items
    "generating":    "Генерируется",
    "generated":     "Готово",
    "error":         "Ошибка",
    # Статусы schedule
    "planned":       "Запланировано",
    "published":     "Опубликовано",
    "manual_pending":"Ждёт ручной публикации",
    "manual_done":   "Опубликовано вручную",
    # Статусы content (старый флоу v1, для таблицы «Вне плана»)
    "draft":         "Черновик",
    "text_review":   "Черновик",
    "production":    "В продакшне",
    "review":        "На ревью",
    "approved":      "Одобрено",
    "rejected":      "Отклонено",
}

# dot-классы для легенды и ячеек
_DOT_CLASS: dict[str, str] = {
    "generating":    "dot-generating",
    "error":         "dot-error",
    "generated":     "dot-generated",
    "planned":       "dot-planned",
    "published":     "dot-published",
    "manual_pending":"dot-manual",
    "manual_done":   "dot-published",
    "pub_error":     "dot-error",
}

_PLATFORM_LABELS = {
    "telegram":  "Telegram",
    "vk":        "ВКонтакте",
    "youtube":   "YouTube",
    "instagram": "Instagram",
    "dzen":      "Яндекс.Дзен",
}

# Статусы schedule, которые нельзя отменить
_CANCEL_FORBIDDEN = {"published", "manual_pending", "manual_done", "publishing"}

# ─── Вспомогательные: работа с файлами ───────────────────────────────────────

# Алиас для обратной совместимости внутри модуля
_delete_content_files = delete_content_files


# ─── Вспомогательные: БД ──────────────────────────────────────────────────────


def _get_projects_list(db) -> list[dict]:
    rows = db.execute(
        "SELECT id, slug, name, stage, settings FROM projects WHERE status='active' ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_project_by_slug(db, slug: str):
    return db.execute(
        "SELECT * FROM projects WHERE slug=? AND status='active'", (slug,)
    ).fetchone()


def _get_item_or_404(db, item_id: int):
    return db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()


def _item_display_dot(db, item: dict) -> str:
    """Вернуть ключ в _DOT_CLASS для ячейки шахматки."""
    if item["status"] == "generating":
        return "generating"
    if item["status"] == "error":
        return "error"
    if item["status"] == "generated":
        if item.get("content_id"):
            srow = db.execute(
                "SELECT status FROM schedule WHERE content_id=? LIMIT 1",
                (item["content_id"],),
            ).fetchone()
            if srow:
                s = srow["status"]
                if s == "error":
                    return "pub_error"
                return s  # planned/published/manual_pending/manual_done
        return "generated"
    return item["status"]


def _build_board_context(db, project: dict, year: int, month: int) -> dict:
    today = date.today()
    days_in_month = _calendar_mod.monthrange(year, month)[1]
    days = list(range(1, days_in_month + 1))

    weekday_abbrs = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    first_weekday = _calendar_mod.weekday(year, month, 1)
    day_headers = []
    for d in days:
        wd = (first_weekday + d - 1) % 7
        is_today = (year == today.year and month == today.month and d == today.day)
        day_headers.append({"day": d, "wd": weekday_abbrs[wd], "today": is_today})

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

    items_by_key: dict[tuple, list] = {}
    platform_types: dict[str, set] = {}

    for row in rows:
        item = dict(row)
        dot_key = _item_display_dot(db, item)
        item["dot_class"] = _DOT_CLASS.get(dot_key, "dot-generated")
        item["status_label"] = _STATUS_LABELS.get(item["status"], item["status"])

        ti = catalog.type_info(item["platform"], item["content_type"])
        item["type_label"] = ti["label"] if ti else item["content_type"]

        day_part = item["date"][8:10]
        try:
            day_int = int(day_part)
        except ValueError:
            continue

        key = (item["platform"], item["content_type"], day_int)
        items_by_key.setdefault(key, []).append(item)
        platform_types.setdefault(item["platform"], set()).add(item["content_type"])

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
                cells.append(items_by_key.get((plat, ctype, d), []))
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
        "dot_class": _DOT_CLASS,
    }


def _get_manual_pending(db, project_id: int) -> list[dict]:
    """Вернуть список schedule со status=manual_pending для проекта."""
    rows = db.execute(
        """
        SELECT s.id AS schedule_id, s.platform, s.planned_at, s.status,
               c.id AS content_id, c.type, c.title, c.texts, c.files
          FROM schedule s
          JOIN content c ON c.id = s.content_id
         WHERE s.status = 'manual_pending'
           AND c.project_id = ?
         ORDER BY s.planned_at ASC
        """,
        (project_id,),
    ).fetchall()

    result = []
    for row in rows:
        item = dict(row)
        try:
            item["texts_parsed"] = json.loads(item["texts"]) if item["texts"] else {}
        except (json.JSONDecodeError, TypeError):
            item["texts_parsed"] = {}
        try:
            item["files_parsed"] = json.loads(item["files"]) if item["files"] else {}
        except (json.JSONDecodeError, TypeError):
            item["files_parsed"] = {}
        item["platform_label"] = _PLATFORM_LABELS.get(item["platform"], item["platform"])
        # Индексы слайдов для сторис (волна 8.5B)
        slides = item["files_parsed"].get("slides") if isinstance(item["files_parsed"], dict) else None
        item["slide_indexes"] = list(range(1, len(slides) + 1)) if isinstance(slides, list) else []
        # Текст для копирования: story → caption; instagram → caption; иначе text
        texts = item["texts_parsed"]
        plat = item["platform"]
        if item["type"] == "story":
            item["copy_text"] = (
                texts.get(plat, {}).get("caption")
                or next((v.get("caption", "") for v in texts.values() if isinstance(v, dict)), "")
            )
        elif plat == "instagram":
            item["copy_text"] = (
                texts.get("instagram", {}).get("caption")
                or texts.get("instagram", {}).get("text")
                or next(
                    (v.get("text", "") for v in texts.values() if isinstance(v, dict)),
                    ""
                )
            )
        else:
            item["copy_text"] = (
                texts.get(plat, {}).get("text")
                or next(
                    (v.get("text", "") for v in texts.values() if isinstance(v, dict)),
                    ""
                )
            )
        result.append(item)
    return result


def _get_orphan_content(db) -> list[dict]:
    """
    Вернуть осиротевший контент: показываем ВСЕ статусы (включая rejected/published
    из старого флоу v1) в двух случаях:
    (а) нет связанного plan_item вообще;
    (б) есть plan_item, но проект архивирован — шахматка /queue показывает только
        активные проекты, значит такой контент иначе нигде не виден.
    """
    rows = db.execute(
        """
        SELECT c.id, c.project_id, c.type, c.title, c.status, c.created_at,
               p.name AS project_name, p.status AS project_status
          FROM content c
          JOIN projects p ON p.id = c.project_id
         WHERE (
               NOT EXISTS (
                   SELECT 1 FROM plan_items pi WHERE pi.content_id = c.id
               )
               OR p.status = 'archived'
         )
         ORDER BY c.created_at DESC
        """,
    ).fetchall()
    return [dict(r) for r in rows]


# ─── Роуты ────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def queue_board(
    request: Request,
    project: str = "",
    month: str = "",
):
    """Главная страница: шахматка + ручная публикация + вне плана.
    Если задан cookie current_project — показываем только его."""
    today = date.today()
    year = today.year
    month_int = today.month
    if month:
        try:
            parts = month.split("-")
            year = max(2020, min(2099, int(parts[0])))
            month_int = max(1, min(12, int(parts[1])))
        except (ValueError, IndexError):
            year, month_int = today.year, today.month

    # Читаем cookie текущего проекта
    cookie_slug = request.cookies.get("current_project", "")

    with get_db() as db:
        all_projects = _get_projects_list(db)

        if project:
            proj_row = _get_project_by_slug(db, project)
            if proj_row is None:
                return HTMLResponse("Проект не найден", status_code=404)
        else:
            proj_row = None

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
                "dot_class": _DOT_CLASS,
                "manual_items": [],
                "orphan_content": [],
                "enable_scheduler": settings.ENABLE_SCHEDULER,
                "cookie_project": None,
            })

        # Выбор проекта:
        # 1. query-параметр ?project=slug (явный выбор)
        # 2. cookie current_project
        # 3. первый running, потом первый active
        if proj_row is None and cookie_slug:
            proj_row = _get_project_by_slug(db, cookie_slug)
        if proj_row is None:
            for p in all_projects:
                if p["stage"] == "running":
                    proj_row = db.execute("SELECT * FROM projects WHERE id=?", (p["id"],)).fetchone()
                    break
            if proj_row is None:
                proj_row = db.execute("SELECT * FROM projects WHERE id=?", (all_projects[0]["id"],)).fetchone()

        proj_dict = dict(proj_row)
        ctx = _build_board_context(db, proj_dict, year, month_int)
        ctx["all_projects"] = all_projects
        ctx["manual_items"] = _get_manual_pending(db, proj_dict["id"])
        ctx["orphan_content"] = _get_orphan_content(db)
        ctx["enable_scheduler"] = settings.ENABLE_SCHEDULER
        ctx["cookie_project"] = cookie_slug if cookie_slug else None

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
        ti = catalog.type_info(item["platform"], item["content_type"])
        item["type_label"] = ti["label"] if ti else item["content_type"]
        item["platform_label"] = _PLATFORM_LABELS.get(item["platform"], item["platform"])

        content = None
        schedule = None
        if item["content_id"]:
            crow = db.execute("SELECT * FROM content WHERE id=?", (item["content_id"],)).fetchone()
            if crow:
                content = dict(crow)
                try:
                    content["texts_parsed"] = json.loads(content["texts"]) if content["texts"] else {}
                except (json.JSONDecodeError, TypeError):
                    content["texts_parsed"] = {}
                try:
                    content["files_parsed"] = json.loads(content["files"]) if content["files"] else {}
                except (json.JSONDecodeError, TypeError):
                    content["files_parsed"] = {}

                srow = db.execute(
                    "SELECT * FROM schedule WHERE content_id=? LIMIT 1",
                    (item["content_id"],),
                ).fetchone()
                if srow:
                    schedule = dict(srow)
                    schedule["status_label"] = _STATUS_LABELS.get(schedule["status"], schedule["status"])

    return templates.TemplateResponse(request, "queue/_modal.html", {
        "item": item,
        "content": content,
        "schedule": schedule,
        "status_labels": _STATUS_LABELS,
    })


@router.post("/items/{plan_item_id}/cancel", response_class=HTMLResponse)
async def queue_item_cancel(plan_item_id: int):
    """Снять с публикации: schedule status=planned → DELETE."""
    with get_db() as db:
        row = _get_item_or_404(db, plan_item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        item = dict(row)
        if not item["content_id"]:
            return HTMLResponse("Нет связанного контента", status_code=422)

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


@router.post("/items/{plan_item_id}/regen", response_class=HTMLResponse)
async def queue_item_regen(request: Request, plan_item_id: int):
    """
    Перегенерировать контент:
    удалить content + файлы + schedule → plan_item → approved (фабрика подберёт).
    """
    with get_db() as db:
        row = _get_item_or_404(db, plan_item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        item = dict(row)
        content_id = item.get("content_id")

        if content_id:
            # Удалить schedule
            db.execute("DELETE FROM schedule WHERE content_id=?", (content_id,))
            # Удалить content
            db.execute("DELETE FROM content WHERE id=?", (content_id,))

        # Сбросить plan_item → approved
        db.execute(
            """
            UPDATE plan_items
            SET status='approved', error_text=NULL, content_id=NULL,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (plan_item_id,),
        )

    # Удалить файлы (после закрытия соединения)
    if content_id:
        _delete_content_files(content_id)

    return HTMLResponse(
        "<div class='alert alert-info'>Поставлено на перегенерацию. "
        "Фабрика подберёт при следующем тике.</div>"
        "<button type='button' class='btn btn-secondary btn-sm' onclick='closeModal()'>Закрыть</button>",
        status_code=200,
    )


@router.post("/items/{plan_item_id}/delete", response_class=HTMLResponse)
async def queue_item_delete(request: Request, plan_item_id: int):
    """
    Удалить контент + файлы + schedule → plan_item → rejected.
    Контент больше не вернётся.
    """
    with get_db() as db:
        row = _get_item_or_404(db, plan_item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        item = dict(row)
        content_id = item.get("content_id")

        if content_id:
            db.execute("DELETE FROM schedule WHERE content_id=?", (content_id,))
            db.execute("DELETE FROM content WHERE id=?", (content_id,))

        db.execute(
            """
            UPDATE plan_items
            SET status='rejected', error_text=NULL, content_id=NULL,
                updated_at=datetime('now')
            WHERE id=?
            """,
            (plan_item_id,),
        )

    if content_id:
        _delete_content_files(content_id)

    return HTMLResponse(
        "<div class='alert alert-warning'>Контент удалён, пункт плана отклонён.</div>"
        "<button type='button' class='btn btn-secondary btn-sm' onclick='closeModal()'>Закрыть</button>",
        status_code=200,
    )


@router.post("/items/{plan_item_id}/retry", response_class=HTMLResponse)
async def queue_item_retry(plan_item_id: int):
    """
    Повторить генерацию: plan_item status='error' → 'approved'.
    Обратная совместимость с тестами волны D.
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


@router.post("/schedules/{schedule_id}/manual-done", response_class=HTMLResponse)
async def queue_manual_done(
    request: Request,
    schedule_id: int,
    published_url: str = Form(default=""),
):
    """Отметить ручную публикацию выполненной."""
    with get_db() as db:
        srow = db.execute(
            "SELECT id, status FROM schedule WHERE id=?", (schedule_id,)
        ).fetchone()
        if srow is None:
            return HTMLResponse("Запись расписания не найдена", status_code=404)
        if srow["status"] not in ("manual_pending",):
            return HTMLResponse(
                f"Ожидается статус manual_pending, текущий: {srow['status']}",
                status_code=422,
            )

    mark_manual_done(schedule_id, published_url.strip())

    return HTMLResponse(
        "<div class='alert alert-info' style='font-size:.9rem;'>Опубликовано вручную ✓</div>",
        status_code=200,
    )


@router.post("/orphans/{content_id}/delete", response_class=HTMLResponse)
async def queue_orphan_delete(content_id: int):
    """Удалить осиротевший контент + файлы + schedule."""
    with get_db() as db:
        crow = db.execute("SELECT id FROM content WHERE id=?", (content_id,)).fetchone()
        if crow is None:
            return HTMLResponse("Контент не найден", status_code=404)

        db.execute("DELETE FROM schedule WHERE content_id=?", (content_id,))
        db.execute("DELETE FROM content WHERE id=?", (content_id,))

    _delete_content_files(content_id)

    return HTMLResponse(
        f"<span style='color:var(--text2);font-size:.85rem;'>Контент #{content_id} удалён</span>",
        status_code=200,
    )


@router.post("/orphans/delete-all", response_class=HTMLResponse)
async def queue_orphan_delete_all():
    """
    Удалить ВЕСЬ осиротевший контент из текущей выборки _get_orphan_content.
    Используется кнопкой «Удалить всё» в секции «Вне плана».
    """
    # Собираем id текущей выборки (тот же запрос, что _get_orphan_content)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT c.id
              FROM content c
              JOIN projects p ON p.id = c.project_id
             WHERE (
                   NOT EXISTS (
                       SELECT 1 FROM plan_items pi WHERE pi.content_id = c.id
                   )
                   OR p.status = 'archived'
             )
            """,
        ).fetchall()
        content_ids = [r["id"] for r in rows]

        for cid in content_ids:
            db.execute("DELETE FROM schedule WHERE content_id=?", (cid,))
            db.execute("DELETE FROM content WHERE id=?", (cid,))

    # Удалить файлы после закрытия соединения
    for cid in content_ids:
        _delete_content_files(cid)

    n = len(content_ids)
    return HTMLResponse(
        f"<div class='alert alert-info'>Удалено {n} ед. контента.</div>",
        status_code=200,
    )
