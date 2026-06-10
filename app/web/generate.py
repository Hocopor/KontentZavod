"""
Веб-роуты для генерации идей и постов.

Маршруты:
    POST /projects/{slug}/ideas/generate  — сгенерировать идеи (HTMX)
    POST /projects/{slug}/ideas/add       — добавить идею вручную
    POST /ideas/{idea_id}/script          — сгенерировать пост по идее
    POST /ideas/{idea_id}/reject          — отклонить идею
"""
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import get_db
from app.llm import LLMError
from app.pipeline.ideas import generate_ideas
from app.pipeline.script import generate_script, generate_script_ab
from app.pipeline.video_script import generate_video_script
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Утилиты ──────────────────────────────────────────────────────────────────


def _get_project_by_slug(slug: str):
    """Возвращает dict проекта или None."""
    with get_db() as db:
        row = db.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


def _get_project_ideas(project_id: int) -> list[dict]:
    """Список идей проекта (свежие сверху)."""
    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM ideas WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _draft_count(project_id: int) -> int:
    """Количество черновиков контента (status='text_review')."""
    with get_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM content WHERE project_id=? AND status='text_review'",
            (project_id,),
        ).fetchone()[0]


# ─── Генерация идей (HTMX POST) ───────────────────────────────────────────────


@router.post("/projects/{slug}/ideas/generate", response_class=HTMLResponse)
async def ideas_generate(request: Request, slug: str):
    """
    HTMX-эндпоинт. Генерирует 5 идей и возвращает HTML-фрагмент
    со списком идей (заменяет #ideas-list).
    """
    project = _get_project_by_slug(slug)
    if project is None:
        return HTMLResponse("<div class='alert alert-error'>Проект не найден</div>", status_code=404)

    try:
        generate_ideas(project["id"], n=5)
    except ValueError as exc:
        return HTMLResponse(
            f"<div class='alert alert-error'>{exc}</div>", status_code=400
        )
    except LLMError as exc:
        logger.error("ideas_generate: LLMError для проекта %s: %s", slug, exc)
        return HTMLResponse(
            f"<div class='alert alert-error'>"
            f"Ошибка генерации: LLM не смог сформировать идеи. Попробуйте ещё раз.<br>"
            f"<small style='opacity:.7'>{exc}</small>"
            f"</div>",
            status_code=502,
        )

    ideas = _get_project_ideas(project["id"])
    draft_count = _draft_count(project["id"])
    return templates.TemplateResponse(
        request,
        "projects/_ideas_list.html",
        {"project": project, "ideas": ideas, "draft_count": draft_count},
    )


# ─── Ручное добавление идеи ───────────────────────────────────────────────────


@router.post("/projects/{slug}/ideas/add", response_class=HTMLResponse)
async def ideas_add(request: Request, slug: str, text: str = Form("")):
    """Добавить идею вручную."""
    project = _get_project_by_slug(slug)
    if project is None:
        return HTMLResponse("<div class='alert alert-error'>Проект не найден</div>", status_code=404)

    text = text.strip()
    if not text:
        return HTMLResponse(
            "<div class='alert alert-error'>Текст идеи не может быть пустым</div>",
            status_code=422,
        )

    with get_db() as db:
        db.execute(
            "INSERT INTO ideas (project_id, text, source, status) VALUES (?,?,?,?)",
            (project["id"], text, "manual", "new"),
        )

    ideas = _get_project_ideas(project["id"])
    draft_count = _draft_count(project["id"])
    return templates.TemplateResponse(
        request,
        "projects/_ideas_list.html",
        {"project": project, "ideas": ideas, "draft_count": draft_count},
    )


# ─── Создать пост по идее ─────────────────────────────────────────────────────


@router.post("/ideas/{idea_id}/script", response_class=HTMLResponse)
async def idea_make_script(request: Request, idea_id: int):
    """Генерирует черновик поста и редиректит на страницу проекта."""
    with get_db() as db:
        idea = db.execute(
            "SELECT i.*, p.slug FROM ideas i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
            (idea_id,),
        ).fetchone()

    if idea is None:
        return HTMLResponse(
            "<div class='alert alert-error'>Идея не найдена</div>", status_code=404
        )

    slug = idea["slug"]

    try:
        generate_script(idea_id)
    except ValueError as exc:
        return HTMLResponse(
            f"<div class='alert alert-error'>{exc}</div>", status_code=400
        )
    except LLMError as exc:
        logger.error("idea_make_script: LLMError для idea_id=%d: %s", idea_id, exc)
        return HTMLResponse(
            f"<div class='alert alert-error'>"
            f"Ошибка генерации поста. Попробуйте ещё раз.<br>"
            f"<small style='opacity:.7'>{exc}</small>"
            f"</div>",
            status_code=502,
        )

    # После успеха — обновить список идей через HTMX (если запрос через HTMX)
    # или редирект на страницу проекта
    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        project_dict = _get_project_by_slug(slug)
        ideas = _get_project_ideas(project_dict["id"])
        draft_count = _draft_count(project_dict["id"])
        return templates.TemplateResponse(
            request,
            "projects/_ideas_list.html",
            {
                "project": project_dict,
                "ideas": ideas,
                "draft_count": draft_count,
                "flash": "Черновик создан, ждёт ревью",
            },
        )

    return RedirectResponse(f"/projects/{slug}#ideas", status_code=303)


# ─── A/B-тест хука: создать два черновика поста ───────────────────────────────


@router.post("/ideas/{idea_id}/script_ab", response_class=HTMLResponse)
async def idea_make_script_ab(request: Request, idea_id: int):
    """Генерирует два черновика поста (A/B-тест хука) по идее."""
    with get_db() as db:
        idea = db.execute(
            "SELECT i.*, p.slug FROM ideas i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
            (idea_id,),
        ).fetchone()

    if idea is None:
        return HTMLResponse(
            "<div class='alert alert-error'>Идея не найдена</div>", status_code=404
        )

    slug = idea["slug"]

    try:
        generate_script_ab(idea_id)
    except ValueError as exc:
        return HTMLResponse(
            f"<div class='alert alert-error'>{exc}</div>", status_code=400
        )
    except LLMError as exc:
        logger.error("idea_make_script_ab: LLMError для idea_id=%d: %s", idea_id, exc)
        return HTMLResponse(
            f"<div class='alert alert-error'>"
            f"Ошибка генерации A/B-поста. Попробуйте ещё раз.<br>"
            f"<small style='opacity:.7'>{exc}</small>"
            f"</div>",
            status_code=502,
        )

    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        project_dict = _get_project_by_slug(slug)
        ideas = _get_project_ideas(project_dict["id"])
        draft_count = _draft_count(project_dict["id"])
        return templates.TemplateResponse(
            request,
            "projects/_ideas_list.html",
            {
                "project": project_dict,
                "ideas": ideas,
                "draft_count": draft_count,
                "flash": "A/B-черновики созданы (вариант A и B), ждут ревью",
            },
        )

    return RedirectResponse(f"/projects/{slug}#ideas", status_code=303)


# ─── Создать видео по идее (футажи или слайдшоу) ─────────────────────────────


async def _idea_make_video(request: Request, idea_id: int, template: str):
    """Общая логика генерации видео-сценария (футажи или слайдшоу)."""
    with get_db() as db:
        idea = db.execute(
            "SELECT i.*, p.slug FROM ideas i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
            (idea_id,),
        ).fetchone()

    if idea is None:
        return HTMLResponse(
            "<div class='alert alert-error'>Идея не найдена</div>", status_code=404
        )

    slug = idea["slug"]

    try:
        generate_video_script(idea_id, template)
    except ValueError as exc:
        return HTMLResponse(
            f"<div class='alert alert-error'>{exc}</div>", status_code=400
        )
    except LLMError as exc:
        logger.error("idea_make_video: LLMError для idea_id=%d: %s", idea_id, exc)
        return HTMLResponse(
            f"<div class='alert alert-error'>"
            f"Ошибка генерации видео-сценария. Попробуйте ещё раз.<br>"
            f"<small style='opacity:.7'>{exc}</small>"
            f"</div>",
            status_code=502,
        )

    is_htmx = request.headers.get("HX-Request") == "true"
    if is_htmx:
        project_dict = _get_project_by_slug(slug)
        ideas = _get_project_ideas(project_dict["id"])
        draft_count = _draft_count(project_dict["id"])
        return templates.TemplateResponse(
            request,
            "projects/_ideas_list.html",
            {
                "project": project_dict,
                "ideas": ideas,
                "draft_count": draft_count,
                "flash": "Видео-сценарий создан, ждёт ревью",
            },
        )

    return RedirectResponse(f"/projects/{slug}#ideas", status_code=303)


@router.post("/ideas/{idea_id}/video_footage", response_class=HTMLResponse)
async def idea_make_video_footage(request: Request, idea_id: int):
    """Генерирует видео-сценарий (тип: футажи) и редиректит на страницу проекта."""
    return await _idea_make_video(request, idea_id, "video_footage")


@router.post("/ideas/{idea_id}/video_slideshow", response_class=HTMLResponse)
async def idea_make_video_slideshow(request: Request, idea_id: int):
    """Генерирует видео-сценарий (тип: слайдшоу) и редиректит на страницу проекта."""
    return await _idea_make_video(request, idea_id, "video_slideshow")


# ─── Отклонить идею ───────────────────────────────────────────────────────────


@router.post("/ideas/{idea_id}/reject", response_class=HTMLResponse)
async def idea_reject(request: Request, idea_id: int):
    """Отклонить идею (status → rejected)."""
    with get_db() as db:
        idea = db.execute(
            "SELECT i.*, p.slug FROM ideas i JOIN projects p ON p.id=i.project_id WHERE i.id=?",
            (idea_id,),
        ).fetchone()

    if idea is None:
        return HTMLResponse(
            "<div class='alert alert-error'>Идея не найдена</div>", status_code=404
        )

    with get_db() as db:
        db.execute(
            "UPDATE ideas SET status='rejected' WHERE id=?", (idea_id,)
        )

    slug = idea["slug"]
    project_dict = _get_project_by_slug(slug)
    ideas = _get_project_ideas(project_dict["id"])
    draft_count = _draft_count(project_dict["id"])
    return templates.TemplateResponse(
        request,
        "projects/_ideas_list.html",
        {"project": project_dict, "ideas": ideas, "draft_count": draft_count},
    )
