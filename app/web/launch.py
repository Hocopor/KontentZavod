"""
Настройки запуска проекта и управление жизненным циклом (этап 7, волна B).

Роуты:
    GET  /projects/{slug}/launch-panel       — фрагмент панели запуска
    POST /projects/{slug}/launch-settings    — сохранить настройки (HTMX)
    POST /projects/{slug}/launch             — запустить проект (stage=running)
    POST /projects/{slug}/stop               — остановить (stage=paused)
    POST /projects/{slug}/toggle-pause/{what} — инвертировать паузу plan/gen/autogen
"""
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import catalog
from app.db import get_db, get_project_settings, DEFAULT_PROJECT_SETTINGS
from app.templates_env import templates

router = APIRouter(prefix="/projects")


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def _get_project_by_slug(db, slug: str):
    """Вернуть строку projects или None."""
    return db.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()


def _get_enabled_platforms(db, project_id: int) -> list[dict]:
    """Вернуть включённые площадки с распарсенными content_types."""
    rows = db.execute(
        "SELECT * FROM project_platforms WHERE project_id=? AND enabled=1 ORDER BY platform",
        (project_id,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        # Разобрать content_types JSON (NULL → используем default_on из каталога)
        ct_json = d.get("content_types")
        if ct_json:
            try:
                d["content_types_parsed"] = json.loads(ct_json)
            except (json.JSONDecodeError, TypeError):
                d["content_types_parsed"] = {}
        else:
            d["content_types_parsed"] = {}
        result.append(d)
    return result


def _get_strategy_status(db, project_id: int) -> dict:
    """
    Вернуть статус последней стратегии проекта.

    Returns:
        dict с ключами: exists (bool), status (str|None), version (int|None),
        error_text (str|None).
    """
    row = db.execute(
        """
        SELECT status, version, error_text
        FROM strategies
        WHERE project_id=?
        ORDER BY version DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if row is None:
        return {"exists": False, "status": None, "version": None, "error_text": None}
    return {
        "exists": True,
        "status": row["status"],
        "version": row["version"],
        "error_text": row["error_text"],
    }


def _clamp(value, lo: int, hi: int) -> int:
    """Привести int к диапазону [lo, hi]."""
    return max(lo, min(hi, value))


def _save_settings(db, project_id: int, form_data: dict) -> dict:
    """
    Сохранить числовые настройки и content_types из формы.

    Возвращает актуальный словарь настроек.
    """
    # --- числовые поля с clamp ---
    def _int(key: str, default: int) -> int:
        try:
            return int(form_data.get(key, default))
        except (ValueError, TypeError):
            return default

    plan_horizon_days = _clamp(_int("plan_horizon_days", 30), 7, 90)
    gen_lookahead_days = _clamp(_int("gen_lookahead_days", 3), 1, 14)
    retention_days = _clamp(_int("retention_days", 14), 3, 60)

    # --- загрузить текущие settings, сохранить paused/autogen ---
    project = db.execute("SELECT settings FROM projects WHERE id=?", (project_id,)).fetchone()
    cur_settings = get_project_settings(project["settings"] if project else None)

    new_settings = {
        **cur_settings,
        "plan_horizon_days": plan_horizon_days,
        "gen_lookahead_days": gen_lookahead_days,
        "retention_days": retention_days,
    }

    db.execute(
        "UPDATE projects SET settings=?, updated_at=datetime('now') WHERE id=?",
        (json.dumps(new_settings), project_id),
    )

    # --- content_types per платформа ---
    # Получить включённые платформы
    platforms_rows = db.execute(
        "SELECT id, platform FROM project_platforms WHERE project_id=? AND enabled=1",
        (project_id,),
    ).fetchall()

    for pl_row in platforms_rows:
        platform = pl_row["platform"]
        allowed = catalog.allowed_types(platform)
        if not allowed:
            continue
        ct_map = {}
        for ctype in allowed:
            field_name = f"ct_{platform}_{ctype}"
            # Чекбокс: если поле есть и не пустое — включён
            val = form_data.get(field_name, "")
            if isinstance(val, list):
                ct_map[ctype] = bool(val)
            else:
                ct_map[ctype] = val in ("1", "on", "true")
        db.execute(
            "UPDATE project_platforms SET content_types=? WHERE id=?",
            (json.dumps(ct_map), pl_row["id"]),
        )

    return new_settings


def _build_launch_context(db, project, error: str | None = None,
                          notice: str | None = None) -> dict:
    """Собрать контекст для рендера _launch.html."""
    settings_data = get_project_settings(project["settings"])
    enabled_platforms = _get_enabled_platforms(db, project["id"])
    strategy_status = _get_strategy_status(db, project["id"])

    # Обогатить платформы данными каталога
    platforms_ctx = []
    for pl in enabled_platforms:
        allowed = catalog.allowed_types(pl["platform"])
        saved_ct = pl["content_types_parsed"]
        types_ctx = []
        for ctype, info in allowed.items():
            # Если нет saved — используем default_on
            enabled_flag = saved_ct.get(ctype, info["default_on"]) if saved_ct else info["default_on"]
            types_ctx.append({
                "key": ctype,
                "label": info["label"],
                "kind": info["kind"],
                "publish": info["publish"],
                "default_on": info["default_on"],
                "enabled": enabled_flag,
            })
        pl_display = {
            "telegram": "Telegram",
            "vk": "ВКонтакте",
            "youtube": "YouTube",
            "instagram": "Instagram",
            "dzen": "Яндекс.Дзен",
        }.get(pl["platform"], pl["platform"])
        platforms_ctx.append({
            "platform": pl["platform"],
            "display_name": pl_display,
            "types": types_ctx,
        })

    return {
        "project": dict(project),
        "settings": settings_data,
        "enabled_platforms": platforms_ctx,
        "strategy": strategy_status,
        "error": error,
        "notice": notice,
    }


# ─── Роуты ────────────────────────────────────────────────────────────────────


@router.get("/{slug}/launch-panel", response_class=HTMLResponse)
async def launch_panel(request: Request, slug: str):
    """Рендер фрагмента панели запуска (lazy-load)."""
    with get_db() as db:
        project = _get_project_by_slug(db, slug)
        if project is None:
            return HTMLResponse("Проект не найден", status_code=404)
        ctx = _build_launch_context(db, project)

    return templates.TemplateResponse(request, "projects/_launch.html", ctx)


@router.post("/{slug}/launch-settings", response_class=HTMLResponse)
async def launch_settings_save(request: Request, slug: str):
    """Сохранить настройки запуска и вернуть обновлённый фрагмент."""
    form = await request.form()
    form_data = dict(form)

    with get_db() as db:
        project = _get_project_by_slug(db, slug)
        if project is None:
            return HTMLResponse("Проект не найден", status_code=404)
        _save_settings(db, project["id"], form_data)
        # Перечитать проект после обновления
        project = _get_project_by_slug(db, slug)
        ctx = _build_launch_context(db, project, notice="✓ Настройки сохранены")

    return templates.TemplateResponse(request, "projects/_launch.html", ctx)


@router.post("/{slug}/launch", response_class=HTMLResponse)
async def launch_project(request: Request, slug: str):
    """
    Запустить проект:
    1. Сохранить настройки из формы.
    2. Проверить: есть ≥1 включённая платформа с ≥1 включённым типом.
    3. Установить stage='running'.
    4. Если нет стратегии active/generating — создать strategies(generating).
    """
    form = await request.form()
    form_data = dict(form)

    with get_db() as db:
        project = _get_project_by_slug(db, slug)
        if project is None:
            return HTMLResponse("Проект не найден", status_code=404)

        _save_settings(db, project["id"], form_data)

        # Проверить наличие платформ с включёнными типами
        enabled_platforms = _get_enabled_platforms(db, project["id"])
        has_valid_platform = False
        for pl in enabled_platforms:
            allowed = catalog.allowed_types(pl["platform"])
            saved_ct = pl["content_types_parsed"]
            for ctype, info in allowed.items():
                enabled_flag = saved_ct.get(ctype, info["default_on"]) if saved_ct else info["default_on"]
                if enabled_flag:
                    has_valid_platform = True
                    break
            if has_valid_platform:
                break

        if not has_valid_platform:
            # Перечитать после сохранения
            project = _get_project_by_slug(db, slug)
            ctx = _build_launch_context(
                db, project,
                error="Необходимо подключить хотя бы одну площадку с включённым типом контента."
            )
            return templates.TemplateResponse(
                request, "projects/_launch.html", ctx, status_code=422
            )

        # Установить stage='running'
        db.execute(
            "UPDATE projects SET stage='running', updated_at=datetime('now') WHERE id=?",
            (project["id"],),
        )

        # Создать стратегию, если нет active или generating
        existing_strategy = db.execute(
            """
            SELECT id FROM strategies
            WHERE project_id=? AND status IN ('active', 'generating')
            LIMIT 1
            """,
            (project["id"],),
        ).fetchone()

        if existing_strategy is None:
            next_version = db.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM strategies WHERE project_id=?",
                (project["id"],),
            ).fetchone()[0]
            db.execute(
                """
                INSERT INTO strategies (project_id, version, status)
                VALUES (?, ?, 'generating')
                """,
                (project["id"], next_version),
            )

        # Перечитать проект после всех изменений
        project = _get_project_by_slug(db, slug)
        ctx = _build_launch_context(db, project)

    return templates.TemplateResponse(request, "projects/_launch.html", ctx)


@router.post("/{slug}/stop", response_class=HTMLResponse)
async def stop_project(request: Request, slug: str):
    """Остановить проект (stage='paused')."""
    with get_db() as db:
        project = _get_project_by_slug(db, slug)
        if project is None:
            return HTMLResponse("Проект не найден", status_code=404)

        db.execute(
            "UPDATE projects SET stage='paused', updated_at=datetime('now') WHERE id=?",
            (project["id"],),
        )
        project = _get_project_by_slug(db, slug)
        ctx = _build_launch_context(db, project)

    return templates.TemplateResponse(request, "projects/_launch.html", ctx)


@router.post("/{slug}/toggle-pause/{what}", response_class=HTMLResponse)
async def toggle_pause(request: Request, slug: str, what: str):
    """
    Инвертировать паузу:
      what=plan    → plan_paused
      what=gen     → gen_paused
      what=autogen → autogen
    """
    if what not in ("plan", "gen", "autogen"):
        return HTMLResponse("Неизвестный параметр", status_code=400)

    with get_db() as db:
        project = _get_project_by_slug(db, slug)
        if project is None:
            return HTMLResponse("Проект не найден", status_code=404)

        settings_data = get_project_settings(project["settings"])

        # Инвертировать нужный флаг
        if what == "plan":
            settings_data["plan_paused"] = 0 if settings_data.get("plan_paused") else 1
        elif what == "gen":
            settings_data["gen_paused"] = 0 if settings_data.get("gen_paused") else 1
        elif what == "autogen":
            settings_data["autogen"] = 0 if settings_data.get("autogen") else 1

        db.execute(
            "UPDATE projects SET settings=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(settings_data), project["id"]),
        )
        project = _get_project_by_slug(db, slug)
        ctx = _build_launch_context(db, project)

    return templates.TemplateResponse(request, "projects/_launch.html", ctx)
