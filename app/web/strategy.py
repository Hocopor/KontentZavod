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
from app.services.cleanup import delete_content_cascade

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


GOAL_LABELS = {
    "attract": "привлечение",
    "retain": "удержание",
    "sell": "продажи",
    "brand": "бренд",
}


def _render_rubrics(raw_rubrics) -> list[dict]:
    """Нормализовать рубрики: поддержать строки (легаси) и объекты {name, goal, description}."""
    out = []
    for r in (raw_rubrics or []):
        if isinstance(r, dict):
            out.append({
                "name": (r.get("name") or "").strip(),
                "goal": r.get("goal") or "",
                "goal_label": GOAL_LABELS.get(r.get("goal"), ""),
                "description": (r.get("description") or "").strip(),
            })
        else:
            # легаси-строка («Название — описание»)
            out.append({"name": str(r), "goal": "", "goal_label": "", "description": ""})
    return out


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
            "rubrics": _render_rubrics(pdata.get("rubrics")),
            "content_mix": mix_rows,
            "best_times": pdata.get("best_times", []) or [],
            "kpi": pdata.get("kpi", ""),
        })
    return out


def _type_label(platform: str, ctype: str) -> str:
    info = catalog.type_info(platform, ctype)
    return info["label"] if info else ctype


def _render_phases(strategy: dict) -> list[dict]:
    """Подготовить фазы для шаблона: подписи целей и человекочитаемый mix."""
    phases = strategy.get("phases") if isinstance(strategy, dict) else None
    if not isinstance(phases, list):
        return []
    out = []
    for ph in phases:
        if not isinstance(ph, dict):
            continue
        gs = ph.get("goal_share") or {}
        goal_share = [
            {"goal": k, "label": GOAL_LABELS.get(k, k), "pct": gs[k]}
            for k in ("attract", "retain", "sell", "brand") if k in gs
        ]
        mix = ph.get("mix") or {}
        mix_lines = []
        for plat in PLATFORMS_ORDER:
            if plat not in mix or not isinstance(mix[plat], dict):
                continue
            parts = [
                f"{n} {_type_label(plat, ctype)}"
                for ctype, n in mix[plat].items()
            ]
            if parts:
                short = PLATFORM_NAMES.get(plat, plat)
                mix_lines.append(f"{short}: {', '.join(parts)}")
        out.append({
            "n": ph.get("n"),
            "name": ph.get("name", ""),
            "weeks": ph.get("weeks"),
            "objective": ph.get("objective", ""),
            "goal_share": goal_share,
            "mix_lines": mix_lines,
            "notes": ph.get("notes", ""),
        })
    return out


def _active_directives(db, project_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT id, text, scope FROM directives "
        "WHERE project_id=? AND status='active' ORDER BY id",
        (project_id,),
    ).fetchall()
    return [{"id": r["id"], "text": r["text"], "scope": r["scope"]} for r in rows]


def _build_context(request: Request, project, *, flash=None, error=None) -> dict:
    with get_db() as db:
        current = _active_strategy(db, project["id"])
        history = _history(db, project["id"])
        directives = _active_directives(db, project["id"])

    strategy_obj = {}
    if current and current["strategy"]:
        try:
            strategy_obj = json.loads(current["strategy"])
        except (json.JSONDecodeError, TypeError):
            strategy_obj = {}

    check_warnings = strategy_obj.get("check_warnings") if isinstance(strategy_obj, dict) else None
    if not isinstance(check_warnings, list):
        check_warnings = []

    return {
        "project": dict(project),
        "current": dict(current) if current else None,
        "status": current["status"] if current else None,
        "strategy": strategy_obj,
        "platforms": _render_platforms(strategy_obj),
        "phases": _render_phases(strategy_obj),
        "directives": directives,
        "check_warnings": check_warnings,
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


@router.post("/reset")
async def strategy_reset(request: Request, slug: str):
    """Полный сброс: удалить все стратегии, весь контент-план и контент; вернуть проект в draft."""
    project = _get_project(slug)
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)

    with get_db() as db:
        # Собрать content_id из plan_items
        content_rows = db.execute(
            "SELECT content_id FROM plan_items WHERE project_id=? AND content_id IS NOT NULL",
            (project["id"],),
        ).fetchall()
        content_ids = [r["content_id"] for r in content_rows]

        # Каскадное удаление контента
        for cid in content_ids:
            delete_content_cascade(db, cid)

        # Удалить все пункты плана
        db.execute("DELETE FROM plan_items WHERE project_id=?", (project["id"],))

        # Удалить все стратегии
        db.execute("DELETE FROM strategies WHERE project_id=?", (project["id"],))

        # Вернуть проект в черновик
        db.execute(
            "UPDATE projects SET stage='draft', updated_at=datetime('now') WHERE id=?",
            (project["id"],),
        )

    return RedirectResponse(f"/projects/{slug}/strategy", status_code=303)


@router.post("/directives/{directive_id}/{action}")
async def strategy_directive_action(
    request: Request, slug: str, directive_id: int, action: str
):
    """Изменить статус директивы: action in ('done', 'dismissed').

    - Проверяет принадлежность директивы проекту (404 если не та).
    - Проверяет action (422 если не done/dismissed).
    - UPDATE directives SET status=? → 303 на страницу стратегии.
    """
    if action not in ("done", "dismissed"):
        return HTMLResponse("Неизвестное действие (допустимо: done, dismissed)", status_code=422)

    project = _get_project(slug)
    if project is None:
        return HTMLResponse("Проект не найден", status_code=404)

    with get_db() as db:
        row = db.execute(
            "SELECT id, project_id FROM directives WHERE id=?", (directive_id,)
        ).fetchone()
        if row is None or row["project_id"] != project["id"]:
            return HTMLResponse("Директива не найдена", status_code=404)

        db.execute(
            "UPDATE directives SET status=? WHERE id=?",
            (action, directive_id),
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
        parsed_rubrics = []
        for line in rubrics_raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if "|" in line:
                parts = line.split("|", 2)
                name = parts[0].strip()
                goal_raw = parts[1].strip() if len(parts) > 1 else ""
                desc = parts[2].strip() if len(parts) > 2 else ""
                goal = goal_raw if goal_raw in ("attract", "retain", "sell", "brand") else "retain"
                parsed_rubrics.append({"name": name, "goal": goal, "description": desc})
            else:
                # легаси-строка: оставить строкой
                parsed_rubrics.append(line)
        pdata["rubrics"] = parsed_rubrics

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
