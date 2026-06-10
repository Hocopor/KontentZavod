"""
Тесты видео-контента в ручной очереди (/manual).

Все тесты работают с FAKE_LLM=1 (из conftest.patch_env).
Видео-файлы создаются как фейк-данные в тестах.
"""
import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import app.config as cfg_module
from app.db import get_db, init_db


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _slug() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _setup_db():
    init_db(cfg_module.settings.db_path_absolute)


def _create_project(db, *, platforms=("telegram", "vk", "instagram"), slug=None):
    """Создаёт проект с указанными включёнными площадками. Возвращает project_id."""
    s = slug or _slug()
    cur = db.execute(
        """
        INSERT INTO projects
            (slug, name, description, audience, tone, goals, cta, themes)
        VALUES (?, 'Тест', 'Описание', 'ЦА', 'тон', 'цели', 'CTA', 'темы')
        """,
        (s,),
    )
    project_id = cur.lastrowid
    for p in ("telegram", "vk", "youtube", "instagram", "dzen"):
        enabled = 1 if p in platforms else 0
        db.execute(
            "INSERT INTO project_platforms (project_id, platform, enabled, mode) VALUES (?,?,?,?)",
            (project_id, p, enabled, "manual" if p == "instagram" else "auto"),
        )
    return project_id


def _create_video_content(
    db,
    project_id: int,
    *,
    title: str = "Тестовое видео",
    content_type: str = "video_footage",
) -> int:
    """Создаёт видео-контент с texts.instagram.caption и files.video_path."""
    texts = json.dumps({
        "instagram": {
            "caption": "Это заголовок видео для Instagram 🎬 #тест",
            "hashtags": ["#видео", "#тест"],
        },
        "telegram": {"text": "Текст для Telegram", "hashtags": ["#тест"]},
        "vk": {"text": "Текст для VK", "hashtags": ["#тест"]},
    }, ensure_ascii=False)

    files = json.dumps({
        "video_path": "/absolute/path/to/video/123.mp4",
        "preview_path": "/absolute/path/to/video/123.jpg",
        "subs_path": "/absolute/path/to/video/123.ass",
    }, ensure_ascii=False)

    features = json.dumps({
        "hook_type": "вопрос",
        "topic": "тест видео",
        "length": "short",
        "format": "reel",
    }, ensure_ascii=False)

    cur = db.execute(
        """
        INSERT INTO content (project_id, type, title, texts, files, features, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, content_type, title, texts, files, features, "approved"),
    )
    return cur.lastrowid


def _create_text_content(
    db,
    project_id: int,
    *,
    title: str = "Тестовый текстовый пост",
) -> int:
    """Создаёт текстовый контент БЕЗ видео (files пусто)."""
    texts = json.dumps({
        "instagram": {
            "caption": "Текстовый пост для Instagram",
            "hashtags": ["#текст", "#тест"],
        },
        "telegram": {"text": "Текст для Telegram", "hashtags": ["#тест"]},
    }, ensure_ascii=False)

    features = json.dumps({
        "hook_type": "вопрос",
        "topic": "текстовый пост",
        "length": "short",
    }, ensure_ascii=False)

    cur = db.execute(
        """
        INSERT INTO content (project_id, type, title, texts, features, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, "post", title, texts, features, "approved"),
    )
    return cur.lastrowid


def _create_schedule(
    db,
    content_id: int,
    platform: str,
    *,
    status: str = "manual_pending",
) -> int:
    """Создаёт запись schedule. Возвращает schedule_id."""
    planned_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute(
        """
        INSERT INTO schedule (content_id, platform, planned_at, status)
        VALUES (?, ?, ?, ?)
        """,
        (content_id, platform, planned_at, status),
    )
    return cur.lastrowid


# ─── Фикстуры ─────────────────────────────────────────────────────────────────


@pytest.fixture
def video_project(patch_env):
    """Создаёт проект с instagram в режиме manual. Возвращает project_id."""
    _setup_db()
    with get_db() as db:
        project_id = _create_project(db, platforms=("instagram",))
    return project_id


# ─── Тесты ────────────────────────────────────────────────────────────────────


class TestManualVideoContent:
    def test_video_content_shows_player_and_download_link(self, client, video_project):
        """Видео-контент в ручной очереди должен отдавать плеер, ссылку и caption."""
        with get_db() as db:
            content_id = _create_video_content(db, video_project)
            schedule_id = _create_schedule(db, content_id, "instagram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200

        # Проверяем наличие видеоплеера
        assert f'<video' in resp.text
        assert f'src="/files/videos/{content_id}.mp4"' in resp.text
        assert f'poster="/files/videos/{content_id}.jpg"' in resp.text

        # Проверяем ссылку для скачивания
        assert f'href="/files/videos/{content_id}.mp4"' in resp.text
        assert 'download' in resp.text

        # Проверяем текст (caption)
        assert "Это заголовок видео для Instagram" in resp.text
        assert "#видео" in resp.text

    def test_video_content_uses_instagram_caption(self, client, video_project):
        """Для Instagram текст должен браться из texts['instagram']['caption']."""
        with get_db() as db:
            content_id = _create_video_content(
                db, video_project,
                title="Видео с Instagram caption",
            )
            _create_schedule(db, content_id, "instagram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200
        # caption из tests.instagram (не text)
        assert "Это заголовок видео для Instagram 🎬 #тест" in resp.text

    def test_non_video_content_has_no_player(self, client, video_project):
        """Текстовый контент БЕЗ видео не должен показывать плеер."""
        with get_db() as db:
            content_id = _create_text_content(db, video_project)
            _create_schedule(db, content_id, "instagram", status="manual_pending")

        resp = client.get("/manual")
        assert resp.status_code == 200

        # Плеера быть не должно
        assert '<video' not in resp.text
        assert f'/files/videos/{content_id}.mp4' not in resp.text

        # Но текст всё ещё есть
        assert "Текстовый пост для Instagram" in resp.text
