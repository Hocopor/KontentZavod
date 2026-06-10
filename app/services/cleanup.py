"""
Очистка медиафайлов после публикации и ротация диска.

cleanup_after_render()  — удалить ассеты (исходники) после рендера видео
rotate_media()          — ротация по политике хранения MEDIA_RETENTION_DAYS
"""
import json
import logging
import shutil
from pathlib import Path

from app.config import settings
from app.db import get_db

logger = logging.getLogger(__name__)


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
    with get_db() as db:
        # Контент, у которого все schedule published/manual_done
        # и самая свежая updated_at старше retention_days
        rows = db.execute(
            """
            SELECT c.id,
                   c.files,
                   COUNT(s.id)                    AS sched_count,
                   SUM(CASE WHEN s.status IN ('published', 'manual_done') THEN 1 ELSE 0 END)
                                                  AS done_count,
                   MAX(s.updated_at)              AS last_updated
              FROM content c
              JOIN schedule s ON s.content_id = c.id
             GROUP BY c.id
            HAVING sched_count > 0
               AND sched_count = done_count
               AND last_updated < datetime('now', '-' || ? || ' days')
            """,
            (retention_days,),
        ).fetchall()

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
