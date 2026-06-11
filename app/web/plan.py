"""
Шахматка согласования контент-плана (этап 7, волна C).

Роуты:
    GET    /plan                          — страница-шахматка
    GET    /plan/item/{id}                — HTMX-фрагмент модалки
    POST   /plan/item/{id}/save           — сохранить правки (title, дата, brief)
    POST   /plan/item/{id}/approve        — одобрить пункт плана
    POST   /plan/item/{id}/reject         — отклонить пункт плана
    DELETE /plan/item/{id}                — удалить пункт (только если content_id IS NULL)
    POST   /plan/approve-period           — одобрить все proposed за месяц
    POST   /plan/autogen/{slug}           — toggle autogen в settings проекта
"""
import calendar
import json
from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import catalog
from app.db import get_db, get_project_settings
from app.templates_env import templates

router = APIRouter(prefix="/plan")

# Русские названия месяцев
_MONTH_NAMES = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# Русские метки статусов план-ячеек
_STATUS_LABELS = {
    "proposed":   "Предложено",
    "approved":   "Одобрено",
    "rejected":   "Отклонено",
    "generating": "Генерируется",
    "generated":  "Готово",
    "error":      "Ошибка",
}

# Цвета статусов (CSS-переменные / hex)
_STATUS_COLORS = {
    "proposed":   "#f59e0b",   # жёлтый
    "approved":   "#22c55e",   # зелёный
    "rejected":   "#64748b",   # серый
    "generating": "#38bdf8",   # голубой
    "generated":  "#6c63ff",   # синий/акцент
    "error":      "#ef4444",   # красный
}

# Отображаемые имена платформ
_PLATFORM_LABELS = {
    "telegram":  "Telegram",
    "vk":        "ВКонтакте",
    "youtube":   "YouTube",
    "instagram": "Instagram",
    "dzen":      "Яндекс.Дзен",
}


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _get_projects_list(db) -> list[dict]:
    """Вернуть активные проекты."""
    rows = db.execute(
        "SELECT id, slug, name, stage, settings FROM projects WHERE status='active' ORDER BY name"
    ).fetchall()
    return [dict(r) for r in rows]


def _get_running_projects_count(db) -> int:
    """Вернуть количество running-проектов."""
    row = db.execute(
        "SELECT COUNT(*) as cnt FROM projects WHERE status='active' AND stage='running'"
    ).fetchone()
    return row["cnt"] if row else 0


def _get_project_by_slug(db, slug: str):
    """Вернуть строку projects или None."""
    return db.execute(
        "SELECT * FROM projects WHERE slug=? AND status='active'", (slug,)
    ).fetchone()


def _count_proposed_in_month(db, project_id: int, year: int, month: int) -> int:
    """Количество proposed пунктов в указанном месяце."""
    month_str = f"{year:04d}-{month:02d}"
    row = db.execute(
        """
        SELECT COUNT(*) as cnt FROM plan_items
        WHERE project_id=? AND date LIKE ? AND status='proposed'
        """,
        (project_id, f"{month_str}-%"),
    ).fetchone()
    return row["cnt"] if row else 0


def _build_board_context(db, project: dict, year: int, month: int) -> dict:
    """
    Собрать контекст для шахматки:
    - список дней месяца
    - строки: платформа → тип (только присутствующие в плане + включённые)
    - ячейки (platform, content_type, day) → список plan_items
    """
    today = date.today()
    days_in_month = calendar.monthrange(year, month)[1]
    days = list(range(1, days_in_month + 1))

    # Дни недели: сокращения на русском
    weekday_abbrs = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

    # Заголовки колонок: (номер дня, день_недели)
    first_weekday = calendar.weekday(year, month, 1)  # 0=пн
    day_headers = []
    for d in days:
        wd = (first_weekday + d - 1) % 7
        is_today = (year == today.year and month == today.month and d == today.day)
        day_headers.append({"day": d, "wd": weekday_abbrs[wd], "today": is_today})

    # Получить план-пункты месяца
    month_str = f"{year:04d}-{month:02d}"
    rows = db.execute(
        """
        SELECT * FROM plan_items
        WHERE project_id=? AND date LIKE ?
        ORDER BY date, time_slot NULLS LAST, id
        """,
        (project["id"], f"{month_str}-%"),
    ).fetchall()

    items_by_key: dict[tuple, list] = {}  # (platform, content_type, day) -> list[dict]
    platform_types: dict[str, set] = {}   # platform -> set of content_types

    for row in rows:
        item = dict(row)
        # Распарсить brief JSON
        try:
            item["brief_parsed"] = json.loads(item["brief"]) if item["brief"] else {}
        except (json.JSONDecodeError, TypeError):
            item["brief_parsed"] = {}
        item["status_label"] = _STATUS_LABELS.get(item["status"], item["status"])
        item["status_color"] = _STATUS_COLORS.get(item["status"], "#888")
        # Лейбл типа контента
        ti = catalog.type_info(item["platform"], item["content_type"])
        item["type_label"] = ti["label"] if ti else item["content_type"]

        # Извлечь день
        day_part = item["date"][8:10]
        try:
            day_int = int(day_part)
        except ValueError:
            continue

        key = (item["platform"], item["content_type"], day_int)
        items_by_key.setdefault(key, []).append(item)

        platform_types.setdefault(item["platform"], set()).add(item["content_type"])

    # Добавить включённые типы из project_platforms (чтобы строки были даже пустые)
    pp_rows = db.execute(
        "SELECT platform, content_types FROM project_platforms WHERE project_id=? AND enabled=1",
        (project["id"],),
    ).fetchall()
    for pp in pp_rows:
        plat = pp["platform"]
        ct_json = pp["content_types"]
        if ct_json:
            try:
                ct_map = json.loads(ct_json)
            except (json.JSONDecodeError, TypeError):
                ct_map = {}
        else:
            ct_map = {}
        allowed = catalog.allowed_types(plat)
        for ctype, info in allowed.items():
            enabled_flag = ct_map.get(ctype, info["default_on"])
            if enabled_flag:
                platform_types.setdefault(plat, set()).add(ctype)

    # Построить строки шахматки: платформы в порядке каталога
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

    # Настройки проекта (autogen / паузы)
    settings = get_project_settings(project.get("settings"))

    # Количество proposed за месяц (для счётчика у кнопки)
    proposed_count = _count_proposed_in_month(db, project["id"], year, month)

    # Навигация месяца
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    return {
        "project": dict(project),
        "year": year,
        "month": month,
        "month_name": _MONTH_NAMES[month],
        "day_headers": day_headers,
        "rows_data": rows_data,
        "settings": settings,
        "status_labels": _STATUS_LABELS,
        "status_colors": _STATUS_COLORS,
        "proposed_count": proposed_count,
        "prev_year": prev_year,
        "prev_month": prev_month,
        "next_year": next_year,
        "next_month": next_month,
        "today": today,
    }


def _get_item_or_404(db, item_id: int):
    """Вернуть plan_item или None."""
    return db.execute("SELECT * FROM plan_items WHERE id=?", (item_id,)).fetchone()


def _render_modal(request: Request, db, item_id: int, error: str | None = None):
    """Рендер фрагмента модалки для item."""
    row = _get_item_or_404(db, item_id)
    if row is None:
        return HTMLResponse("Пункт плана не найден", status_code=404)
    item = dict(row)
    try:
        item["brief_parsed"] = json.loads(item["brief"]) if item["brief"] else {}
    except (json.JSONDecodeError, TypeError):
        item["brief_parsed"] = {}
    item["status_label"] = _STATUS_LABELS.get(item["status"], item["status"])
    item["status_color"] = _STATUS_COLORS.get(item["status"], "#888")
    ti = catalog.type_info(item["platform"], item["content_type"])
    item["type_label"] = ti["label"] if ti else item["content_type"]
    item["platform_label"] = _PLATFORM_LABELS.get(item["platform"], item["platform"])

    return templates.TemplateResponse(request, "plan/_modal.html", {
        "item": item,
        "error": error,
    })


# ─── Роуты ────────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
async def plan_board(
    request: Request,
    project: str = "",
    year: int = 0,
    month: int = 0,
):
    """
    Главная страница шахматки.
    ?project=slug&year=2026&month=6
    """
    today = date.today()
    if year == 0:
        year = today.year
    if month == 0:
        month = today.month

    # Ограничить диапазон
    year = max(2020, min(2099, year))
    month = max(1, min(12, month))

    with get_db() as db:
        all_projects = _get_projects_list(db)
        running_count = _get_running_projects_count(db)

        if not all_projects:
            return templates.TemplateResponse(request, "plan/board.html", {
                "all_projects": [],
                "project": None,
                "running_count": running_count,
                "year": year,
                "month": month,
                "month_name": _MONTH_NAMES[month],
                "rows_data": [],
                "day_headers": [],
                "settings": {},
                "status_labels": _STATUS_LABELS,
                "status_colors": _STATUS_COLORS,
                "proposed_count": 0,
                "prev_year": year if month > 1 else year - 1,
                "prev_month": month - 1 if month > 1 else 12,
                "next_year": year if month < 12 else year + 1,
                "next_month": month + 1 if month < 12 else 1,
                "today": today,
            })

        # Выбор проекта: из query или первый running/active
        proj_row = None
        if project:
            proj_row = _get_project_by_slug(db, project)
        if proj_row is None:
            # Предпочитаем running
            for p in all_projects:
                if p["stage"] == "running":
                    proj_row = db.execute("SELECT * FROM projects WHERE id=?", (p["id"],)).fetchone()
                    break
            if proj_row is None:
                proj_row = db.execute("SELECT * FROM projects WHERE id=?", (all_projects[0]["id"],)).fetchone()

        ctx = _build_board_context(db, dict(proj_row), year, month)
        ctx["all_projects"] = all_projects
        ctx["running_count"] = running_count

    return templates.TemplateResponse(request, "plan/board.html", ctx)


@router.get("/item/{item_id}", response_class=HTMLResponse)
async def plan_item_modal(request: Request, item_id: int):
    """HTMX-фрагмент: модалка просмотра/редактирования пункта плана."""
    with get_db() as db:
        return _render_modal(request, db, item_id)


@router.post("/item/{item_id}/save", response_class=HTMLResponse)
async def plan_item_save(request: Request, item_id: int):
    """
    Сохранить правки пункта плана.
    Поля: title, date, time_slot, hook, outline, cta, keywords, rubric.
    """
    form = await request.form()

    title = (form.get("title") or "").strip()
    date_val = (form.get("date") or "").strip()
    time_slot = (form.get("time_slot") or "").strip()
    hook = (form.get("hook") or "").strip()
    outline = (form.get("outline") or "").strip()
    cta = (form.get("cta") or "").strip()
    keywords_raw = (form.get("keywords") or "").strip()
    rubric = (form.get("rubric") or "").strip()

    # Валидация даты
    error = None
    if date_val:
        try:
            date.fromisoformat(date_val)
        except ValueError:
            error = "Неверный формат даты (ожидается YYYY-MM-DD)."

    # Валидация time_slot
    if not error and time_slot:
        parts = time_slot.split(":")
        if len(parts) != 2:
            error = "Неверный формат времени (ожидается HH:MM)."
        else:
            try:
                h, m = int(parts[0]), int(parts[1])
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    error = "Время вне диапазона (00:00–23:59)."
            except ValueError:
                error = "Неверный формат времени (ожидается HH:MM)."

    with get_db() as db:
        row = _get_item_or_404(db, item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        if error:
            return _render_modal(request, db, item_id, error=error)

        # Распарсить keywords
        keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]

        # Обновить brief
        try:
            old_brief = json.loads(row["brief"]) if row["brief"] else {}
        except (json.JSONDecodeError, TypeError):
            old_brief = {}
        new_brief = {
            **old_brief,
            "hook": hook,
            "outline": outline,
            "cta": cta,
            "keywords": keywords,
            "rubric": rubric,
        }

        db.execute(
            """
            UPDATE plan_items
            SET title=?, date=?, time_slot=?, brief=?, updated_at=datetime('now')
            WHERE id=?
            """,
            (
                title or row["title"],
                date_val or row["date"],
                time_slot or row["time_slot"],
                json.dumps(new_brief, ensure_ascii=False),
                item_id,
            ),
        )

    # Вернуть обновлённую модалку
    with get_db() as db:
        return _render_modal(request, db, item_id)


@router.post("/item/{item_id}/approve", response_class=HTMLResponse)
async def plan_item_approve(request: Request, item_id: int):
    """Одобрить пункт плана (proposed/rejected/error → approved)."""
    with get_db() as db:
        row = _get_item_or_404(db, item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        if row["status"] in ("proposed", "rejected", "error"):
            db.execute(
                "UPDATE plan_items SET status='approved', updated_at=datetime('now') WHERE id=?",
                (item_id,),
            )

        return _render_modal(request, db, item_id)


@router.post("/item/{item_id}/reject", response_class=HTMLResponse)
async def plan_item_reject(request: Request, item_id: int):
    """Отклонить пункт плана (любой статус → rejected)."""
    with get_db() as db:
        row = _get_item_or_404(db, item_id)
        if row is None:
            return HTMLResponse("Пункт плана не найден", status_code=404)

        db.execute(
            "UPDATE plan_items SET status='rejected', updated_at=datetime('now') WHERE id=?",
            (item_id,),
        )

        return _render_modal(request, db, item_id)


@router.delete("/item/{item_id}", response_class=JSONResponse)
async def plan_item_delete(request: Request, item_id: int):
    """
    Удалить пункт плана.
    Разрешено только если content_id IS NULL (контент ещё не создан).
    Если контент уже создан — 422 с человекочитаемым сообщением.
    """
    with get_db() as db:
        row = _get_item_or_404(db, item_id)
        if row is None:
            return JSONResponse({"error": "Пункт плана не найден"}, status_code=404)

        if row["content_id"] is not None:
            return JSONResponse(
                {"error": "Контент уже создан — управляйте им во вкладке Публикация"},
                status_code=422,
            )

        db.execute("DELETE FROM plan_items WHERE id=?", (item_id,))

    return JSONResponse({"ok": True}, status_code=200)


@router.post("/approve-period", response_class=HTMLResponse)
async def approve_period(
    request: Request,
    project_id: int = Form(...),
    year: int = Form(...),
    month: int = Form(...),
):
    """Одобрить все proposed пункты плана за месяц."""
    month_str = f"{year:04d}-{month:02d}"

    with get_db() as db:
        db.execute(
            """
            UPDATE plan_items
            SET status='approved', updated_at=datetime('now')
            WHERE project_id=? AND date LIKE ? AND status='proposed'
            """,
            (project_id, f"{month_str}-%"),
        )
        # Получить slug проекта для редиректа
        proj = db.execute("SELECT slug FROM projects WHERE id=?", (project_id,)).fetchone()

    slug = proj["slug"] if proj else ""
    return RedirectResponse(
        url=f"/plan?project={slug}&year={year}&month={month}",
        status_code=303,
    )


@router.post("/autogen/{slug}", response_class=HTMLResponse)
async def toggle_autogen(request: Request, slug: str):
    """
    Toggle autogen в settings проекта (кнопка «⚡ Генерация одобренного: ВКЛ/ВЫКЛ»).
    Включить — autogen=1, повторный нажатие — autogen=0.
    """
    with get_db() as db:
        proj = db.execute(
            "SELECT id, settings FROM projects WHERE slug=? AND status='active'", (slug,)
        ).fetchone()
        if proj is None:
            return HTMLResponse("Проект не найден", status_code=404)

        settings_data = get_project_settings(proj["settings"])
        settings_data["autogen"] = 0 if settings_data.get("autogen") else 1

        db.execute(
            "UPDATE projects SET settings=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(settings_data), proj["id"]),
        )

    # Получить query-параметры из заголовка Referer или вернуть на /plan
    referer = request.headers.get("referer", "")
    if not referer:
        referer = "/plan"
    return RedirectResponse(url=referer, status_code=303)
