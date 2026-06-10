"""
Календарь публикаций.

Маршруты:
    GET  /calendar                        — месяц (по умолчанию)
    GET  /calendar?view=week              — неделя
    GET  /calendar?year=Y&month=M         — конкретный месяц
    GET  /calendar/entry/{schedule_id}    — детали записи
    POST /calendar/entry/{schedule_id}/reschedule — перенести
    POST /calendar/entry/{schedule_id}/cancel     — отменить (только planned)
    POST /calendar/entry/{schedule_id}/retry      — повторить (только error)
"""
import calendar
import logging
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calendar")

# Цвета статусов
STATUS_COLORS = {
    "planned":        "var(--text2)",          # серый
    "publishing":     "var(--yellow)",          # жёлтый
    "published":      "var(--green)",           # зелёный
    "error":          "var(--red)",             # красный
    "manual_pending": "#f97316",               # оранжевый
    "manual_done":    "#15803d",               # тёмно-зелёный
}

STATUS_LABELS = {
    "planned":        "Запланировано",
    "publishing":     "Публикуется",
    "published":      "Опубликовано",
    "error":          "Ошибка",
    "manual_pending": "Ожидает ручной публикации",
    "manual_done":    "Опубликовано вручную",
}

PLATFORM_NAMES = {
    "telegram": "TG",
    "vk": "VK",
    "youtube": "YT",
    "instagram": "IG",
    "dzen": "Дзен",
}

RU_MONTHS = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _load_schedule_rows(year: int, month: int) -> list[dict]:
    """Загрузить все записи schedule за заданный месяц."""
    start = f"{year:04d}-{month:02d}-01 00:00:00"
    # Последний день месяца
    last_day = calendar.monthrange(year, month)[1]
    end = f"{year:04d}-{month:02d}-{last_day:02d} 23:59:59"

    with get_db() as db:
        rows = db.execute(
            """
            SELECT s.id, s.content_id, s.platform, s.planned_at,
                   s.status, s.published_url, s.error_text, s.attempts,
                   c.title, c.type,
                   p.name AS project_name, p.slug AS project_slug
              FROM schedule s
              JOIN content c ON c.id = s.content_id
              JOIN projects p ON p.id = c.project_id
             WHERE s.planned_at BETWEEN ? AND ?
             ORDER BY s.planned_at
            """,
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def _load_week_rows(week_start: date) -> list[dict]:
    """Загрузить записи schedule за неделю."""
    week_end = week_start + timedelta(days=6)
    start = f"{week_start} 00:00:00"
    end = f"{week_end} 23:59:59"

    with get_db() as db:
        rows = db.execute(
            """
            SELECT s.id, s.content_id, s.platform, s.planned_at,
                   s.status, s.published_url, s.error_text, s.attempts,
                   c.title, c.type,
                   p.name AS project_name, p.slug AS project_slug
              FROM schedule s
              JOIN content c ON c.id = s.content_id
              JOIN projects p ON p.id = c.project_id
             WHERE s.planned_at BETWEEN ? AND ?
             ORDER BY s.planned_at
            """,
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def _enrich_row(row: dict) -> dict:
    """Добавить вычисляемые поля к записи schedule."""
    row["color"] = STATUS_COLORS.get(row["status"], "var(--text2)")
    row["status_label"] = STATUS_LABELS.get(row["status"], row["status"])
    row["platform_short"] = PLATFORM_NAMES.get(row["platform"], row["platform"])
    title = row.get("title") or ""
    row["title_short"] = (title[:30] + "…") if len(title) > 30 else title
    # День для группировки в сетке
    if row.get("planned_at"):
        try:
            row["day"] = int(row["planned_at"][8:10])
        except (ValueError, IndexError):
            row["day"] = 0
    return row


def _load_approved_unscheduled() -> list[dict]:
    """Контент со status='approved', у которого нет ни одной записи schedule."""
    with get_db() as db:
        rows = db.execute(
            """
            SELECT c.id, c.title, c.type, c.updated_at,
                   p.name AS project_name, p.slug AS project_slug
              FROM content c
              JOIN projects p ON p.id = c.project_id
             WHERE c.status = 'approved'
               AND NOT EXISTS (
                   SELECT 1 FROM schedule s WHERE s.content_id = c.id
               )
             ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Главный вид (месяц / неделя) ─────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def calendar_view(
    request: Request,
    view: str = "month",
    year: int = 0,
    month: int = 0,
    week: str = "",
):
    today = date.today()
    flash = request.query_params.get("flash", "")

    if view == "week":
        # Определяем начало недели
        if week:
            try:
                week_start = date.fromisoformat(week)
            except ValueError:
                week_start = today - timedelta(days=today.weekday())
        else:
            week_start = today - timedelta(days=today.weekday())

        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        rows = [_enrich_row(r) for r in _load_week_rows(week_start)]

        # Группируем по дням
        days_data = []
        for d in week_dates:
            day_rows = [r for r in rows if r["planned_at"][:10] == d.isoformat()]
            days_data.append({"date": d, "rows": day_rows})

        prev_week = (week_start - timedelta(days=7)).isoformat()
        next_week = (week_start + timedelta(days=7)).isoformat()

        unscheduled = _load_approved_unscheduled()

        return templates.TemplateResponse(
            request,
            "calendar/week.html",
            {
                "view": "week",
                "week_start": week_start,
                "week_dates": week_dates,
                "days_data": days_data,
                "today": today,
                "prev_week": prev_week,
                "next_week": next_week,
                "ru_weekdays": RU_WEEKDAYS,
                "status_labels": STATUS_LABELS,
                "status_colors": STATUS_COLORS,
                "unscheduled": unscheduled,
                "flash": flash,
            },
        )

    # Месяц по умолчанию
    if not year or not month:
        year = today.year
        month = today.month

    # Сетка месяца
    cal = calendar.monthcalendar(year, month)
    rows = [_enrich_row(r) for r in _load_schedule_rows(year, month)]

    # Группируем по дням
    days_map: dict[int, list[dict]] = {}
    for r in rows:
        days_map.setdefault(r["day"], []).append(r)

    # Навигация
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1

    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    unscheduled = _load_approved_unscheduled()

    return templates.TemplateResponse(
        request,
        "calendar/month.html",
        {
            "view": "month",
            "year": year,
            "month": month,
            "month_name": RU_MONTHS[month],
            "cal": cal,
            "days_map": days_map,
            "today": today,
            "prev_year": prev_year,
            "prev_month": prev_month,
            "next_year": next_year,
            "next_month": next_month,
            "ru_weekdays": RU_WEEKDAYS,
            "status_labels": STATUS_LABELS,
            "status_colors": STATUS_COLORS,
            "unscheduled": unscheduled,
            "flash": flash,
        },
    )


# ─── Детали записи ────────────────────────────────────────────────────────────


@router.get("/entry/{schedule_id}", response_class=HTMLResponse)
async def entry_detail(request: Request, schedule_id: int):
    with get_db() as db:
        row = db.execute(
            """
            SELECT s.id, s.content_id, s.platform, s.planned_at,
                   s.status, s.published_url, s.error_text, s.attempts,
                   c.title, c.type, c.texts,
                   p.name AS project_name, p.slug AS project_slug
              FROM schedule s
              JOIN content c ON c.id = s.content_id
              JOIN projects p ON p.id = c.project_id
             WHERE s.id = ?
            """,
            (schedule_id,),
        ).fetchone()

    if row is None:
        return HTMLResponse("Запись не найдена", status_code=404)

    entry = _enrich_row(dict(row))
    flash = request.query_params.get("flash", "")

    return templates.TemplateResponse(
        request,
        "calendar/entry.html",
        {"entry": entry, "flash": flash},
    )


# ─── Перенести запись ─────────────────────────────────────────────────────────


@router.post("/entry/{schedule_id}/reschedule")
async def entry_reschedule(
    request: Request,
    schedule_id: int,
    planned_at: str = Form(...),
):
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM schedule WHERE id=?", (schedule_id,)
        ).fetchone()

    if row is None:
        return HTMLResponse("Запись не найдена", status_code=404)

    if row["status"] not in ("planned", "error"):
        return RedirectResponse(
            f"/calendar/entry/{schedule_id}?flash=Нельзя+перенести+запись+со+статусом+{row['status']}",
            status_code=303,
        )

    try:
        dt = datetime.fromisoformat(planned_at)
    except ValueError:
        return RedirectResponse(
            f"/calendar/entry/{schedule_id}?flash=Неверный+формат+даты",
            status_code=303,
        )

    with get_db() as db:
        db.execute(
            """
            UPDATE schedule
               SET planned_at=?, status='planned', updated_at=datetime('now')
             WHERE id=?
            """,
            (dt.strftime("%Y-%m-%d %H:%M:%S"), schedule_id),
        )

    return RedirectResponse(
        f"/calendar/entry/{schedule_id}?flash=Перенесено",
        status_code=303,
    )


# ─── Отменить запись ─────────────────────────────────────────────────────────


@router.post("/entry/{schedule_id}/cancel")
async def entry_cancel(schedule_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM schedule WHERE id=?", (schedule_id,)
        ).fetchone()

    if row is None:
        return HTMLResponse("Запись не найдена", status_code=404)

    if row["status"] != "planned":
        return RedirectResponse(
            f"/calendar/entry/{schedule_id}?flash=Отменить+можно+только+запланированную+запись",
            status_code=303,
        )

    with get_db() as db:
        db.execute("DELETE FROM schedule WHERE id=?", (schedule_id,))

    return RedirectResponse("/calendar?flash=Запись+удалена", status_code=303)


# ─── Повторить (retry) ────────────────────────────────────────────────────────


@router.post("/entry/{schedule_id}/retry")
async def entry_retry(schedule_id: int):
    with get_db() as db:
        row = db.execute(
            "SELECT status FROM schedule WHERE id=?", (schedule_id,)
        ).fetchone()

    if row is None:
        return HTMLResponse("Запись не найдена", status_code=404)

    if row["status"] != "error":
        return RedirectResponse(
            f"/calendar/entry/{schedule_id}?flash=Повторить+можно+только+запись+со+статусом+error",
            status_code=303,
        )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        db.execute(
            """
            UPDATE schedule
               SET status='planned', attempts=0, planned_at=?,
                   error_text=NULL, updated_at=datetime('now')
             WHERE id=?
            """,
            (now_str, schedule_id),
        )

    return RedirectResponse(
        f"/calendar/entry/{schedule_id}?flash=Поставлено+на+повтор",
        status_code=303,
    )
