"""
Базовый модуль публикации.

publish(schedule_id) — главная точка входа:
  - Загружает schedule + content + project_platform из БД
  - Расшифровывает credentials
  - Выбирает паблишер по платформе
  - Возвращает published_url

DRY-RUN режим (PUBLISH_DRY_RUN=1 или credentials=None):
  - Не ходит в сеть
  - Сохраняет payload в data/outbox/{schedule_id}_{platform}.json
  - Возвращает dry-run://{platform}/{schedule_id}
"""
import json
import logging
from pathlib import Path

import app.config as _cfg
from app.db import get_db
from app.security import decrypt

logger = logging.getLogger(__name__)

# ─── Исключение ───────────────────────────────────────────────────────────────


class PublishError(Exception):
    """Ошибка публикации — человекочитаемое описание."""


# ─── DRY-RUN helpers ──────────────────────────────────────────────────────────


def _outbox_dir() -> Path:
    """Папка data/outbox/ (создаётся при необходимости)."""
    p = _cfg.settings.data_dir_absolute / "outbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def dry_run_publish(schedule_id: int, platform: str, payload: dict) -> str:
    """
    Сохранить payload в outbox и вернуть dry-run URL.
    Используется и из base.publish() при DRY-RUN, и из конкретных паблишеров.
    """
    out_path = _outbox_dir() / f"{schedule_id}_{platform}.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("DRY-RUN: payload сохранён в %s", out_path)
    return f"dry-run://{platform}/{schedule_id}"


# ─── Главная точка входа ──────────────────────────────────────────────────────


def publish(schedule_id: int) -> str:
    """
    Опубликовать запись по schedule_id.

    Возвращает published_url (строка).
    Поднимает PublishError при ошибке публикации.
    """
    with get_db() as db:
        # Загружаем schedule
        row_s = db.execute(
            "SELECT * FROM schedule WHERE id = ?", (schedule_id,)
        ).fetchone()
        if row_s is None:
            raise PublishError(f"Запись schedule id={schedule_id} не найдена")

        platform: str = row_s["platform"]
        content_id: int = row_s["content_id"]

        # Загружаем content
        row_c = db.execute(
            "SELECT * FROM content WHERE id = ?", (content_id,)
        ).fetchone()
        if row_c is None:
            raise PublishError(f"Контент content id={content_id} не найден")

        project_id: int = row_c["project_id"]

        # Загружаем project_platform
        row_pp = db.execute(
            "SELECT * FROM project_platforms WHERE project_id = ? AND platform = ?",
            (project_id, platform),
        ).fetchone()
        if row_pp is None:
            raise PublishError(
                f"Площадка {platform} не найдена для проекта id={project_id}"
            )

    # Расшифровываем credentials (None если не заданы)
    credentials: dict | None = None
    raw_creds = row_pp["credentials"]
    if raw_creds:
        try:
            credentials = json.loads(decrypt(raw_creds))
        except Exception as exc:
            logger.warning("Не удалось расшифровать credentials для %s: %s", platform, exc)
            credentials = None

    # Разбираем config площадки
    config: dict = {}
    raw_config = row_pp["config"]
    if raw_config:
        try:
            config = json.loads(raw_config)
        except Exception:
            config = {}

    # Разбираем texts контента
    texts: dict = {}
    raw_texts = row_c["texts"]
    if raw_texts:
        try:
            texts = json.loads(raw_texts)
        except Exception:
            texts = {}

    # Определяем режим публикации
    mode: str = row_pp["mode"]
    is_dry_run: bool = _cfg.settings.PUBLISH_DRY_RUN or (credentials is None)

    # Платформы, которые всегда manual на этапе 2
    ALWAYS_MANUAL = {"instagram", "dzen"}

    # Выбираем паблишер
    if platform in ALWAYS_MANUAL or mode == "manual":
        from app.publishers.manual import publish_manual
        return publish_manual(schedule_id, platform, texts)

    if platform == "telegram":
        from app.publishers.telegram import publish_telegram
        return publish_telegram(
            schedule_id=schedule_id,
            credentials=credentials,
            config=config,
            texts=texts,
            files=_parse_files(row_c["files"]),
            dry_run=is_dry_run,
        )

    if platform == "vk":
        from app.publishers.vk import publish_vk
        return publish_vk(
            schedule_id=schedule_id,
            credentials=credentials,
            config=config,
            texts=texts,
            dry_run=is_dry_run,
        )

    # Неизвестная платформа — dry-run заглушка
    logger.warning("Неизвестная платформа %s — dry-run заглушка", platform)
    return dry_run_publish(schedule_id, platform, {"platform": platform, "texts": texts})


def _parse_files(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}
