"""
Страница аналитики публикаций.

Маршруты:
    GET  /analytics                         — сводная страница с графиками и топ-контентом
    POST /analytics/metrics/{schedule_id}   — ручной ввод метрик (IG, Дзен, TG)
"""
import json
import logging
import time
from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.analytics.collector import collect_metrics
from app.db import get_db
from app.templates_env import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics")

# Троттлинг ручного сбора метрик (защита от дабл-клика / частого F5).
_last_collect_ts: float = 0.0
_COLLECT_THROTTLE_SEC = 20.0

PLATFORM_NAMES = {
    "telegram": "Telegram",
    "vk": "ВКонтакте",
    "youtube": "YouTube",
    "instagram": "Instagram",
    "dzen": "Яндекс.Дзен",
}

PLATFORM_COLORS = {
    "telegram": "#38bdf8",
    "vk": "#60a5fa",
    "youtube": "#f87171",
    "instagram": "#f9a8d4",
    "dzen": "#fdba74",
}


def _date_from(days: int) -> str:
    """Возвращает дату начала периода (ISO строка)."""
    return (date.today() - timedelta(days=days)).isoformat()


def _parse_features(features_json: str | None) -> dict:
    try:
        return json.loads(features_json) if features_json else {}
    except (json.JSONDecodeError, TypeError):
        return {}


CONTENT_TYPE_NAMES = {
    "post": "Пост",
    "story": "История",
    "article": "Статья",
    "video_footage": "Видео (футаж)",
    "video_slideshow": "Видео (слайдшоу)",
}


@router.get("", response_class=HTMLResponse)
async def analytics_page(
    request: Request,
    project_id: str = "",
    days: int = 30,
    content_type: str = "",
):
    if days not in (7, 30, 90):
        days = 30
    since = _date_from(days)

    # Валидация content_type
    valid_types = set(CONTENT_TYPE_NAMES.keys())
    if content_type and content_type not in valid_types:
        content_type = ""

    with get_db() as db:
        # Все активные проекты для фильтра
        projects = db.execute(
            "SELECT id, name FROM projects WHERE status='active' ORDER BY name"
        ).fetchall()

        # ── Базовый фильтр по проекту ─────────────────────────────────────────
        proj_filter = "AND p.id = :pid" if project_id else ""
        proj_params: dict = {"since": since, "pid": project_id} if project_id else {"since": since}

        # ── Фильтр по типу контента ───────────────────────────────────────────
        type_filter = "AND c.type = :ctype" if content_type else ""
        if content_type:
            proj_params["ctype"] = content_type

        # ── Сводные карточки ──────────────────────────────────────────────────
        summary = db.execute(
            f"""
            SELECT
                COUNT(DISTINCT s.id)            AS total_pubs,
                COALESCE(SUM(m.views),    0)    AS total_views,
                COALESCE(SUM(m.likes),    0)    AS total_likes,
                COALESCE(SUM(m.comments), 0)    AS total_comments,
                COALESCE(SUM(m.shares),   0)    AS total_shares
            FROM schedule s
            JOIN content c  ON c.id = s.content_id
            JOIN projects p ON p.id = c.project_id
            LEFT JOIN (
                SELECT schedule_id, MAX(date) AS last_date
                FROM metrics
                WHERE date >= :since
                GROUP BY schedule_id
            ) ld ON ld.schedule_id = s.id
            LEFT JOIN metrics m
                ON m.schedule_id = s.id AND m.date = ld.last_date
            WHERE s.status IN ('published', 'manual_done')
              AND s.planned_at >= :since
              {proj_filter}
              {type_filter}
            """,
            proj_params,
        ).fetchone()

        # ── Динамика просмотров по дням, разбивка по платформам ──────────────
        # Суммарные просмотры за каждый день (последний замер каждой публикации)
        timeline_rows = db.execute(
            f"""
            SELECT
                m.date              AS day,
                s.platform          AS platform,
                SUM(m.views)        AS views
            FROM metrics m
            JOIN schedule s ON s.id = m.schedule_id
            JOIN content c  ON c.id = s.content_id
            JOIN projects p ON p.id = c.project_id
            JOIN (
                SELECT schedule_id, MAX(date) AS last_date
                FROM metrics
                WHERE date >= :since
                GROUP BY schedule_id
            ) ld ON ld.schedule_id = m.schedule_id AND m.date = ld.last_date
            WHERE m.date >= :since
              AND s.status IN ('published', 'manual_done')
              {proj_filter}
              {type_filter}
            GROUP BY m.date, s.platform
            ORDER BY m.date
            """,
            proj_params,
        ).fetchall()

        # ── Средние просмотры по платформам (для bar-графика) ─────────────────
        platform_avg_rows = db.execute(
            f"""
            SELECT
                s.platform          AS platform,
                COUNT(DISTINCT s.id) AS pub_count,
                COALESCE(AVG(m.views), 0) AS avg_views
            FROM schedule s
            JOIN content c  ON c.id = s.content_id
            JOIN projects p ON p.id = c.project_id
            LEFT JOIN (
                SELECT schedule_id, MAX(date) AS last_date
                FROM metrics
                WHERE date >= :since
                GROUP BY schedule_id
            ) ld ON ld.schedule_id = s.id
            LEFT JOIN metrics m
                ON m.schedule_id = s.id AND m.date = ld.last_date
            WHERE s.status IN ('published', 'manual_done')
              AND s.planned_at >= :since
              {proj_filter}
              {type_filter}
            GROUP BY s.platform
            ORDER BY avg_views DESC
            """,
            proj_params,
        ).fetchall()

        # ── Разрез по типам контента ──────────────────────────────────────────
        type_breakdown_rows = db.execute(
            f"""
            SELECT
                c.type              AS content_type,
                COUNT(DISTINCT s.id) AS pub_count,
                COALESCE(AVG(m.views),    0) AS avg_views,
                COALESCE(AVG(m.likes),    0) AS avg_likes,
                COALESCE(AVG(m.comments), 0) AS avg_comments,
                COALESCE(AVG(m.shares),   0) AS avg_shares
            FROM schedule s
            JOIN content c  ON c.id = s.content_id
            JOIN projects p ON p.id = c.project_id
            LEFT JOIN (
                SELECT schedule_id, MAX(date) AS last_date
                FROM metrics
                WHERE date >= :since
                GROUP BY schedule_id
            ) ld ON ld.schedule_id = s.id
            LEFT JOIN metrics m
                ON m.schedule_id = s.id AND m.date = ld.last_date
            WHERE s.status IN ('published', 'manual_done')
              AND s.planned_at >= :since
              {proj_filter}
            GROUP BY c.type
            ORDER BY avg_views DESC
            """,
            proj_params,
        ).fetchall()

        # ── Топ и анти-топ контента ────────────────────────────────────────────
        content_metrics_rows = db.execute(
            f"""
            SELECT
                s.id                AS schedule_id,
                s.platform          AS platform,
                s.published_url     AS published_url,
                c.title             AS title,
                c.features          AS features,
                COALESCE(m.views,    0) AS views,
                COALESCE(m.likes,    0) AS likes,
                COALESCE(m.comments, 0) AS comments,
                COALESCE(m.shares,   0) AS shares,
                COALESCE(m.watch_pct, 0.0) AS watch_pct
            FROM schedule s
            JOIN content c  ON c.id = s.content_id
            JOIN projects p ON p.id = c.project_id
            LEFT JOIN (
                SELECT schedule_id, MAX(date) AS last_date
                FROM metrics
                WHERE date >= :since
                GROUP BY schedule_id
            ) ld ON ld.schedule_id = s.id
            LEFT JOIN metrics m
                ON m.schedule_id = s.id AND m.date = ld.last_date
            WHERE s.status IN ('published', 'manual_done')
              AND s.planned_at >= :since
              {proj_filter}
              {type_filter}
            ORDER BY views DESC
            """,
            proj_params,
        ).fetchall()

        # ── Публикации для ручного ввода метрик ───────────────────────────────
        manual_rows = db.execute(
            f"""
            SELECT
                s.id                AS schedule_id,
                s.platform          AS platform,
                s.planned_at        AS planned_at,
                s.published_url     AS published_url,
                c.title             AS title,
                c.id                AS content_id
            FROM schedule s
            JOIN content c  ON c.id = s.content_id
            JOIN projects p ON p.id = c.project_id
            WHERE s.status IN ('published', 'manual_done')
              AND s.planned_at >= :since
              {proj_filter}
              {type_filter}
            ORDER BY s.planned_at DESC
            """,
            proj_params,
        ).fetchall()

        # Последние метрики для предзаполнения форм
        schedule_ids = [r["schedule_id"] for r in manual_rows]
        last_metrics: dict[int, dict] = {}
        if schedule_ids:
            placeholders = ",".join("?" * len(schedule_ids))
            metric_rows = db.execute(
                f"""
                SELECT m.*
                FROM metrics m
                JOIN (
                    SELECT schedule_id, MAX(date) AS last_date
                    FROM metrics
                    WHERE schedule_id IN ({placeholders})
                    GROUP BY schedule_id
                ) ld ON ld.schedule_id = m.schedule_id AND m.date = ld.last_date
                """,
                schedule_ids,
            ).fetchall()
            for mr in metric_rows:
                last_metrics[mr["schedule_id"]] = dict(mr)

    # ── Формируем Chart.js данные ─────────────────────────────────────────────
    # Собираем все дни периода
    all_days = []
    day_cursor = date.today() - timedelta(days=days - 1)
    while day_cursor <= date.today():
        all_days.append(day_cursor.isoformat())
        day_cursor += timedelta(days=1)

    # timeline_map: {platform: {day: views}}
    platforms_found: set[str] = set()
    timeline_map: dict[str, dict[str, int]] = {}
    for row in timeline_rows:
        p = row["platform"]
        platforms_found.add(p)
        if p not in timeline_map:
            timeline_map[p] = {}
        timeline_map[p][row["day"]] = row["views"]

    line_datasets = []
    for p in sorted(platforms_found):
        color = PLATFORM_COLORS.get(p, "#94a3b8")
        line_datasets.append({
            "label": PLATFORM_NAMES.get(p, p),
            "data": [timeline_map[p].get(d, 0) for d in all_days],
            "borderColor": color,
            "backgroundColor": color + "33",
            "tension": 0.3,
            "fill": False,
        })

    # Bar-данные: средние просмотры по платформам
    bar_labels = [PLATFORM_NAMES.get(r["platform"], r["platform"]) for r in platform_avg_rows]
    bar_data = [round(r["avg_views"], 1) for r in platform_avg_rows]
    bar_colors = [PLATFORM_COLORS.get(r["platform"], "#94a3b8") for r in platform_avg_rows]

    chart_data = {
        "labels": all_days,
        "line_datasets": line_datasets,
        "bar_labels": bar_labels,
        "bar_data": bar_data,
        "bar_colors": bar_colors,
    }

    # ── Топ-5 и анти-топ-5 ────────────────────────────────────────────────────
    all_content = []
    for row in content_metrics_rows:
        features = _parse_features(row["features"])
        badges = []
        if features.get("hook_type"):
            badges.append(features["hook_type"])
        if features.get("format"):
            badges.append(features["format"])
        if features.get("ab_variant") and features["ab_variant"] not in (None, "null", ""):
            badges.append(f"A/B:{features['ab_variant']}")
        all_content.append({
            "schedule_id": row["schedule_id"],
            "platform": row["platform"],
            "platform_name": PLATFORM_NAMES.get(row["platform"], row["platform"]),
            "published_url": row["published_url"],
            "title": row["title"] or "Без заголовка",
            "views": row["views"],
            "likes": row["likes"],
            "comments": row["comments"],
            "shares": row["shares"],
            "watch_pct": row["watch_pct"],
            "badges": badges,
        })

    top5 = all_content[:5]
    anti5 = list(reversed(all_content[-5:])) if len(all_content) >= 2 else []
    # Если всего <= 5 записей — анти-топ = те же, что не попали в топ
    if len(all_content) <= 5:
        anti5 = list(reversed(all_content))

    # ── Формируем список для ручного ввода ────────────────────────────────────
    manual_list = []
    for row in manual_rows:
        m = last_metrics.get(row["schedule_id"], {})
        manual_list.append({
            "schedule_id": row["schedule_id"],
            "platform": row["platform"],
            "platform_name": PLATFORM_NAMES.get(row["platform"], row["platform"]),
            "planned_at": row["planned_at"],
            "published_url": row["published_url"] or "",
            "title": row["title"] or "Без заголовка",
            "views": m.get("views", 0),
            "likes": m.get("likes", 0),
            "comments": m.get("comments", 0),
            "shares": m.get("shares", 0),
            "watch_pct": m.get("watch_pct", 0.0),
        })

    summary_dict = dict(summary) if summary else {
        "total_pubs": 0,
        "total_views": 0,
        "total_likes": 0,
        "total_comments": 0,
        "total_shares": 0,
    }

    # ── Разрез по типам контента ──────────────────────────────────────────────
    type_breakdown = []
    for row in type_breakdown_rows:
        ctype = row["content_type"]
        type_breakdown.append({
            "content_type": ctype,
            "type_name": CONTENT_TYPE_NAMES.get(ctype, ctype),
            "pub_count": row["pub_count"],
            "avg_views": round(row["avg_views"], 1),
            "avg_likes": round(row["avg_likes"], 1),
            "avg_comments": round(row["avg_comments"], 1),
            "avg_shares": round(row["avg_shares"], 1),
        })

    flash = request.query_params.get("flash", "")
    error = request.query_params.get("error", "")

    return templates.TemplateResponse(
        request,
        "analytics/index.html",
        {
            "projects": [dict(p) for p in projects],
            "selected_project": project_id,
            "days": days,
            "content_type": content_type,
            "content_type_names": CONTENT_TYPE_NAMES,
            "summary": summary_dict,
            "chart_data": chart_data,
            "top5": top5,
            "anti5": anti5,
            "type_breakdown": type_breakdown,
            "manual_list": manual_list,
            "flash": flash,
            "error": error,
            "platform_names": PLATFORM_NAMES,
        },
    )


@router.post("/collect", response_class=HTMLResponse)
def analytics_collect_now(request: Request):
    """Ручной запуск сбора метрик (HTMX). Возвращает фрагмент _collect.html с результатом."""
    global _last_collect_ts
    now = time.monotonic()
    if now - _last_collect_ts < _COLLECT_THROTTLE_SEC:
        return templates.TemplateResponse(
            request,
            "analytics/_collect.html",
            {"collect_result": "⏳ Только что обновляли — подождите немного."},
        )
    _last_collect_ts = now
    try:
        result = collect_metrics()
        msg = (
            f"✓ Собрано: {result['collected']}, "
            f"пропущено: {result['skipped']}, ошибок: {result['errors']}"
        )
    except Exception as exc:
        logger.warning("Ручной сбор метрик упал: %s", exc)
        msg = "✗ Ошибка сбора метрик (см. логи)"
    return templates.TemplateResponse(
        request,
        "analytics/_collect.html",
        {"collect_result": msg},
    )


@router.post("/metrics/{schedule_id}")
async def save_metrics(
    request: Request,
    schedule_id: int,
    views: int = Form(0),
    likes: int = Form(0),
    comments: int = Form(0),
    shares: int = Form(0),
    watch_pct: float = Form(0.0),
):
    """Ручной ввод метрик — INSERT OR REPLACE на сегодняшнюю дату."""
    # Проверяем, что schedule существует
    with get_db() as db:
        row = db.execute(
            "SELECT id FROM schedule WHERE id=? AND status IN ('published','manual_done')",
            (schedule_id,),
        ).fetchone()

    if row is None:
        return HTMLResponse("Публикация не найдена", status_code=404)

    today = date.today().isoformat()

    with get_db() as db:
        db.execute(
            """
            INSERT INTO metrics (schedule_id, date, views, likes, comments, shares, watch_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(schedule_id, date) DO UPDATE SET
                views     = excluded.views,
                likes     = excluded.likes,
                comments  = excluded.comments,
                shares    = excluded.shares,
                watch_pct = excluded.watch_pct
            """,
            (schedule_id, today, views, likes, comments, shares, watch_pct),
        )

    # Сохраняем параметры фильтра для редиректа обратно
    params = request.query_params
    project_id = params.get("project_id", "")
    days_param = params.get("days", "30")

    redirect_url = f"/analytics?flash=Метрики+сохранены"
    if project_id:
        redirect_url += f"&project_id={project_id}"
    redirect_url += f"&days={days_param}"

    return RedirectResponse(redirect_url, status_code=303)
