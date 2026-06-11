"""
Очистка медиафайлов после публикации и ротация диска.

delete_content_files()    — удалить медиафайлы одной единицы контента
delete_content_cascade()  — удалить schedule + content + файлы по content_id
cleanup_after_render()    — удалить ассеты (исходники) после рендера видео
rotate_media()            — ротация по политике хранения MEDIA_RETENTION_DAYS
"""
import json
import logging
import shutil
from pathlib import Path

from app.config import settings
from app.db import get_db, get_project_settings

logger = logging.getLogger(__name__)


# ─── Удаление файлов одной единицы контента ──────────────────────────────────


def delete_content_files(content_id: int) -> None:
    """
    Удалить media-папку и финальные файлы для content_id.
    Вызывается после DELETE FROM content — когда соединение уже закрыто.
    Вызывается из web/queue.py и web/projects.py.
    """
    data_dir = settings.data_dir_absolute
    # media/{content_id}/
    media_dir = data_dir / "media" / str(content_id)
    if media_dir.exists():
        shutil.rmtree(media_dir, ignore_errors=True)
        logger.debug("cleanup: удалена media/%s/", content_id)
    # videos/{content_id}.mp4 / .jpg
    for ext in ("mp4", "jpg"):
        p = data_dir / "videos" / f"{content_id}.{ext}"
        if p.exists():
            p.unlink(missing_ok=True)
            logger.debug("cleanup: удалён videos/%s.%s", content_id, ext)
    # images/{content_id}.jpg (story)
    img = data_dir / "images" / f"{content_id}.jpg"
    if img.exists():
        img.unlink(missing_ok=True)
        logger.debug("cleanup: удалён images/%s.jpg", content_id)


# ─── Каскадное удаление одной единицы контента ───────────────────────────────


def delete_content_cascade(db, content_id: int) -> None:
    """
    Каскадно удалить одну единицу контента внутри открытой транзакции.

    Порядок:
      1. DELETE FROM schedule WHERE content_id=?
      2. DELETE FROM content WHERE id=?
      3. delete_content_files(content_id)  — файлы удаляются здесь же,
         т.к. в существующих паттернах файлы чистят в рамках той же
         операции (см. queue.py::queue_item_regen).

    Параметры:
        db         — открытое соединение (контекст get_db или переданное).
        content_id — ID удаляемого контента.
    """
    db.execute("DELETE FROM schedule WHERE content_id=?", (content_id,))
    db.execute("DELETE FROM content WHERE id=?", (content_id,))
    delete_content_files(content_id)
    logger.debug("delete_content_cascade: content_id=%s удалён каскадом", content_id)


# ─── Очистка после рендера ────────────────────────────────────────────────────


def cleanup_after_render(content_id: int) -> None:
    """
    Удалить поддиректорию assets/ после рендера видео.

    Удаляем только исходники-футажи: media/{content_id}/assets/.
    Голосовые дорожки (.wav/.mp3) и субтитры (.srt) в media/{content_id}/
    остаются — они мелкие и нужны для повторного рендера.

    Параметры:
        content_id — id контента в БД
    """
    assets_dir = settings.data_dir_absolute / "media" / str(content_id) / "assets"
    if assets_dir.exists():
        shutil.rmtree(assets_dir, ignore_errors=True)
        logger.info("cleanup_after_render: удалена %s", assets_dir)
    else:
        logger.debug("cleanup_after_render: %s не существует, пропуск", assets_dir)


# ─── Ротация диска ────────────────────────────────────────────────────────────


def rotate_media() -> dict:
    """
    Ротация медиафайлов по политике хранения.

    Правила:
    (а) Контент, у которого:
        - есть хотя бы одна запись в schedule
        - ВСЕ записи schedule в статусах published или manual_done
        - максимальный schedule.updated_at старше MEDIA_RETENTION_DAYS дней
        → удалить videos/{content_id}.mp4, videos/{content_id}.jpg,
          media/{content_id}/ целиком;
          в content.files выставить {"purged": true}

    (б) Осиротевшие директории media/{id}/, где id нет в content → удалить.

    Возвращает dict со счётчиками:
        {"purged_content": N, "orphan_dirs": M}
    """
    retention_days = settings.MEDIA_RETENTION_DAYS
    data_dir = settings.data_dir_absolute
    videos_dir = data_dir / "videos"
    media_dir = data_dir / "media"

    purged_count = 0
    orphan_count = 0

    # ── (а) Устаревший опубликованный контент ─────────────────────────────────
    # Загружаем контент без фильтра по retention_days (у каждого проекта свой срок)
    with get_db() as db:
        rows = db.execute(
            """
            SELECT c.id,
                   c.files,
                   c.project_id,
                   COUNT(s.id)                    AS sched_count,
                   SUM(CASE WHEN s.status IN ('published', 'manual_done') THEN 1 ELSE 0 END)
                                                  AS done_count,
                   MAX(s.updated_at)              AS last_updated
              FROM content c
              JOIN schedule s ON s.content_id = c.id
             GROUP BY c.id
            HAVING sched_count > 0
               AND sched_count = done_count
            """,
        ).fetchall()

        # Кэш настроек проектов (project_id → retention_days)
        _project_settings_cache: dict[int, int] = {}
        for row in rows:
            pid = row["project_id"]
            if pid not in _project_settings_cache:
                proj_row = db.execute(
                    "SELECT settings FROM projects WHERE id=?", (pid,)
                ).fetchone()
                proj_settings_json = proj_row["settings"] if proj_row else None
                ps = get_project_settings(proj_settings_json)
                _project_settings_cache[pid] = int(ps.get("retention_days", retention_days))

    # Фильтрация по per-project retention_days
    from datetime import datetime as _dt
    _now = _dt.utcnow()

    def _is_expired(row) -> bool:
        proj_retention = _project_settings_cache.get(row["project_id"], retention_days)
        last_updated = row["last_updated"]
        if not last_updated:
            return False
        try:
            last_dt = _dt.fromisoformat(last_updated)
        except (ValueError, TypeError):
            return False
        return (_now - last_dt).days >= proj_retention

    rows = [r for r in rows if _is_expired(r)]

    for row in rows:
        content_id: int = row["id"]
        files_raw: str | None = row["files"]

        logger.info(
            "rotate_media: контент id=%s (last_updated=%s) → чистка",
            content_id,
            row["last_updated"],
        )

        # Удаляем финальное видео и превью
        for ext in ("mp4", "jpg"):
            f = videos_dir / f"{content_id}.{ext}"
            if f.exists():
                f.unlink()
                logger.debug("rotate_media: удалён %s", f)

        # Удаляем media/{content_id}/ целиком
        media_content_dir = media_dir / str(content_id)
        if media_content_dir.exists():
            shutil.rmtree(media_content_dir, ignore_errors=True)
            logger.debug("rotate_media: удалён каталог %s", media_content_dir)

        # Проставляем purged=true в content.files
        try:
            files: dict = json.loads(files_raw) if files_raw else {}
        except (json.JSONDecodeError, TypeError):
            files = {}
        files["purged"] = True

        with get_db() as db:
            db.execute(
                "UPDATE content SET files = ?, updated_at = datetime('now') WHERE id = ?",
                (json.dumps(files, ensure_ascii=False), content_id),
            )

        purged_count += 1

    # ── (б) Осиротевшие директории media/{id}/ ───────────────────────────────
    if media_dir.exists():
        # Получаем все id контента из БД
        with get_db() as db:
            content_ids = {
                str(r["id"])
                for r in db.execute("SELECT id FROM content").fetchall()
            }

        for child in media_dir.iterdir():
            if child.is_dir() and child.name not in content_ids:
                shutil.rmtree(child, ignore_errors=True)
                logger.info("rotate_media: удалена осиротевшая директория %s", child)
                orphan_count += 1

    logger.info(
        "rotate_media: purged_content=%d, orphan_dirs=%d",
        purged_count,
        orphan_count,
    )
    return {"purged_content": purged_count, "orphan_dirs": orphan_count}
