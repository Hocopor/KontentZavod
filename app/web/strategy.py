"""
Маркетинговая стратегия проекта (этап 7, волна B).

Роуты:
    GET  /projects/{slug}/strategy          — страница стратегии (статус/просмотр/история)
    POST /projects/{slug}/strategy/retry    — повторить генерацию после error
    POST /projects/{slug}/strategy/edit     — ручная правка активной стратегии
    POST /projects/{slug}/strategy/revise   — пересмотр: новая версия generating + user_comment

Рендер активной стратегии: summary, positioning, карточка по каждой платформе
(цели, рубрики, контент-микс таблицей, лучшее время, KPI), changes_summary, история версий.
Generating-блок автообновляется через HTMX (hx-trigger="every 5s").
"""
import json
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import catalog
from app.db import get_db
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{slug}/strategy")

PLATFORM_NAMES = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "dzen": "Яндекс.Дзен",
}
PLATFORMS_ORDER = ["telegram", "vk", "youtube", "instagram", "dzen"]


# ─── Хелперы ──────────────────────────────────────────────────────────────────


def _get_project(slug: str):
    with get_db() as db:
        return db.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()


def _active_strategy(db, project_id: int):
    """Активная стратегия (или генерящаяся/ошибочная — самая свежая нефинальная)."""
    return db.execute(
        "SELECT * FROM strategies WHERE project_id=? "
        "ORDER BY version DESC LIMIT 1",
        (project_id,),
    ).fetchone()


def _history(db, project_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT version, status, user_comment, created_at FROM strategies "
        "WHERE project_id=? ORDER BY version DESC",
        (project_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _render_platforms(strategy: dict) -> list[dict]:
    """Подготовить список платформ для шаблона (с человекочитаемыми названиями типов)."""
    platforms = strategy.get("platforms", {}) if isinstance(strategy, dict) else {}
    ordered = sorted(
        platforms.keys(),
        key=lambda p: PLATFORMS_ORDER.index(p) if p in PLATFORMS_ORDER else 99,
    )
    out = []
    for p in ordered:
        pdata = platforms[p] or {}
        mix = pdata.get("content_mix", {}) or {}
        mix_rows = []
        for ctype, n in mix.items():
            info = catalog.type_info(p, ctype)
            mix_rows.append({
                "type": ctype,
                "label": info["label"] if info else ctype,
                "count": n,
            })
        out.append({
            "platform": p,
            "name": PLATFORM_NAMES.get(p, p),
            "goals": pdata.get("goals", ""),
            "rubrics": pdata.get("rubrics", []) or [],
            "content_mix": mix_rows,
            "best_times": pdata.get("best_times", []) or [],
            "kpi": pdata.get("kpi", ""),
        })
    return out


def _build_context(request: Request, project, *, flash=None, error=None) -> dict:
    with get_db() as db:
        current = _active_strategy(db, project["id"])
        history = _history(db, project["id"])

    strategy_obj = {}
    if current and current["strategy"]:
        try:
            strategy_obj = json.loads(current["strategy"])
        except (json.JSONDecodeError, TypeError):
            strategy_obj = {}

    return {
        "project": dict(project),
        "current": dict(current) if current else None,
        "status": current["status"] if current else None,
        "strategy": strategy_obj,
        "platforms": _render_platforms(strategy_obj),
        "changes_summary": strategy_obj.get("changes_summary"),
        "history": history,
        "flash": flash,
        "error": error,
        "platform_names": PLATFORM_NAMES,
    }


# ─── Роуты ────────────────────────────────────────────────────────────────────


@router.get("")
async def strategy_page(request: Request, slug: str):
    project = _get_project(slug)
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)
    ctx = _build_context(request, project)
    # HTMX-запрос (автообновление статус-блока) → отдаём только фрагмент
    if request.headers.get("HX-Request") and ctx["status"] == "generating":
        return templates.TemplateResponse(request, "strategy/_status.html", ctx)
    return templates.TemplateResponse(request, "strategy/view.html", ctx)


@router.post("/retry")
async def strategy_retry(request: Request, slug: str):
    project = _get_project(slug)
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)
    with get_db() as db:
        current = _active_strategy(db, project["id"])
        if current and current["status"] == "error":
            db.execute(
                "UPDATE strategies SET status='generating', error_text=NULL WHERE id=?",
                (current["id"],),
            )
    return RedirectResponse(f"/projects/{slug}/strategy", status_code=303)


@router.post("/revise")
async def strategy_revise(request: Request, slug: str, user_comment: str = Form("")):
    """Создать новую версию (version+1) status='generating' с комментарием.
    Текущая active остаётся active — архивируется только при успешном build."""
    project = _get_project(slug)
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)
    with get_db() as db:
        row = db.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM strategies WHERE project_id=?",
            (project["id"],),
        ).fetchone()
        next_version = (row["v"] or 0) + 1
        db.execute(
            "INSERT INTO strategies (project_id, version, status, user_comment) "
            "VALUES (?, ?, 'generating', ?)",
            (project["id"], next_version, user_comment.strip() or None),
        )
    return RedirectResponse(f"/projects/{slug}/strategy", status_code=303)


@router.post("/edit")
async def strategy_edit(request: Request, slug: str):
    """Ручная правка активной стратегии. Поля формы:
    summary, positioning, и per-платформа:
      goals_<p>, kpi_<p>, rubrics_<p> (по строке), best_times_<p> (через запятую),
      mix_<p>_<type> (число)."""
    project = _get_project(slug)
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)

    form = await request.form()

    with get_db() as db:
        current = db.execute(
            "SELECT * FROM strategies WHERE project_id=? AND status='active' "
            "ORDER BY version DESC LIMIT 1",
            (project["id"],),
        ).fetchone()

    if current is None:
        ctx = _build_context(request, project, error="Нет активной стратегии для правки.")
        return templates.TemplateResponse(request, "strategy/view.html", ctx, status_code=400)

    try:
        strategy = json.loads(current["strategy"]) if current["strategy"] else {}
    except (json.JSONDecodeError, TypeError):
        strategy = {}
    if not isinstance(strategy, dict):
        strategy = {}

    strategy["summary"] = (form.get("summary") or "").strip()
    strategy["positioning"] = (form.get("positioning") or "").strip()

    platforms = strategy.get("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}

    errors: list[str] = []
    for p, pdata in platforms.items():
        if not isinstance(pdata, dict):
            continue
        pdata["goals"] = (form.get(f"goals_{p}") or "").strip()
        pdata["kpi"] = (form.get(f"kpi_{p}") or "").strip()

        rubrics_raw = form.get(f"rubrics_{p}") or ""
        pdata["rubrics"] = [r.strip() for r in rubrics_raw.splitlines() if r.strip()]

        times_raw = form.get(f"best_times_{p}") or ""
        pdata["best_times"] = [t.strip() for t in times_raw.split(",") if t.strip()]

        # content_mix: только валидные/включённые типы; число ≥ 0
        new_mix: dict = {}
        for ctype in catalog.allowed_types(p):
            field = f"mix_{p}_{ctype}"
            if field in form:
                val = (form.get(field) or "").strip()
                if val == "":
                    continue
                try:
                    num = int(val)
                except ValueError:
                    errors.append(f"{PLATFORM_NAMES.get(p, p)} / {ctype}: «{val}» не число.")
                    continue
                if num < 0:
                    errors.append(f"{PLATFORM_NAMES.get(p, p)} / {ctype}: число не может быть отрицательным.")
                    continue
                if num > 0:
                    new_mix[ctype] = num
        pdata["content_mix"] = new_mix
        platforms[p] = pdata

    strategy["platforms"] = platforms

    if errors:
        ctx = _build_context(request, project, error=" ".join(errors))
        return templates.TemplateResponse(request, "strategy/view.html", ctx, status_code=400)

    with get_db() as db:
        db.execute(
            "UPDATE strategies SET strategy=? WHERE id=?",
            (json.dumps(strategy, ensure_ascii=False), current["id"]),
        )
    return RedirectResponse(f"/projects/{slug}/strategy", status_code=303)
