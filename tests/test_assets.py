"""
Тесты модуля app/pipeline/assets.py и app/services/cleanup.py (этап 3.2).

pytest tests/test_assets.py -q
"""
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─── Утилиты ──────────────────────────────────────────────────────────────────


def _make_scenes(n: int = 2) -> list[dict]:
    return [
        {"text": f"Сцена {i + 1}", "keywords": ["nature", "sunset", "sky"]}
        for i in range(n)
    ]


def _setup_db(db_path: Path) -> None:
    """Инициализировать схему БД для тестов cleanup."""
    from app.db import init_db
    init_db(db_path)


# ─── FAKE_ASSETS: оба шаблона ─────────────────────────────────────────────────

ffmpeg_available = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="ffmpeg не найден в PATH",
)


@ffmpeg_available
def test_fake_assets_video_footage(tmp_path, monkeypatch):
    """FAKE_ASSETS=1 + video_footage → mp4-файлы в порядке сцен."""
    import app.config as cfg_module

    monkeypatch.setenv("FAKE_ASSETS", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.assets import fetch_scene_assets

    scenes = _make_scenes(3)
    paths = fetch_scene_assets(content_id=1, scenes=scenes, template="video_footage")

    assert len(paths) == 3
    for idx, p in enumerate(paths):
        assert p.exists(), f"Файл не создан: {p}"
        assert p.suffix == ".mp4", f"Ожидался .mp4, получен {p.suffix}"
        assert p.name == f"scene_{idx + 1:02d}.mp4", f"Неверное имя: {p.name}"
        assert p.stat().st_size > 0, f"Файл пустой: {p}"


@ffmpeg_available
def test_fake_assets_video_slideshow(tmp_path, monkeypatch):
    """FAKE_ASSETS=1 + video_slideshow → jpg-файлы в порядке сцен."""
    import app.config as cfg_module

    monkeypatch.setenv("FAKE_ASSETS", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.assets import fetch_scene_assets

    scenes = _make_scenes(2)
    paths = fetch_scene_assets(content_id=42, scenes=scenes, template="video_slideshow")

    assert len(paths) == 2
    for idx, p in enumerate(paths):
        assert p.exists(), f"Файл не создан: {p}"
        assert p.suffix == ".jpg", f"Ожидался .jpg, получен {p.suffix}"
        assert p.name == f"scene_{idx + 1:02d}.jpg", f"Неверное имя: {p.name}"
        assert p.stat().st_size > 0, f"Файл пустой: {p}"


@ffmpeg_available
def test_fake_assets_directory_structure(tmp_path, monkeypatch):
    """Ассеты создаются в правильной директории media/{content_id}/assets/."""
    monkeypatch.setenv("FAKE_ASSETS", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.assets import fetch_scene_assets

    paths = fetch_scene_assets(content_id=7, scenes=_make_scenes(1), template="video_slideshow")
    expected_dir = tmp_path / "media" / "7" / "assets"
    assert paths[0].parent == expected_dir


# ─── Pexels: монкипатч httpx ──────────────────────────────────────────────────


def _make_pexels_response(files: list[dict]) -> MagicMock:
    """Собрать fake-ответ Pexels Videos API."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "videos": [
            {
                "id": 1,
                "video_files": files,
            }
        ]
    }
    return resp


def _portrait_file(height: int = 1920, width: int = 1080) -> dict:
    return {
        "id": 1,
        "quality": "hd",
        "width": width,
        "height": height,
        "link": "https://example.com/video.mp4",
    }


def test_pexels_portrait_selected(tmp_path, monkeypatch):
    """Pexels отдаёт портретный файл → скачивается нужный файл."""
    monkeypatch.setenv("FAKE_ASSETS", "0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PEXELS_API_KEY", "fake-pexels-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "")

    portrait = _portrait_file(height=1920, width=1080)
    pexels_resp = _make_pexels_response([portrait])

    # Счётчик вызовов для проверки что скачивание произошло
    downloaded_urls = []

    def fake_stream(method, url, **kwargs):
        downloaded_urls.append(url)
        # Контекстный менеджер — нужен как __enter__/__exit__
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.raise_for_status = MagicMock()
        ctx.iter_bytes = MagicMock(return_value=[b"FAKE_VIDEO_DATA"])
        return ctx

    import httpx
    monkeypatch.setattr(httpx, "get", lambda url, **kw: pexels_resp)
    monkeypatch.setattr(httpx, "stream", fake_stream)

    from app.pipeline.assets import fetch_scene_assets

    paths = fetch_scene_assets(
        content_id=10,
        scenes=[{"text": "Сцена 1", "keywords": ["nature"]}],
        template="video_footage",
    )

    assert len(paths) == 1
    assert paths[0].suffix == ".mp4"
    assert paths[0].exists()
    assert len(downloaded_urls) == 1
    assert downloaded_urls[0] == "https://example.com/video.mp4"


def test_pexels_empty_fallback_pixabay(tmp_path, monkeypatch):
    """Pexels возвращает пустой список → фоллбэк на Pixabay."""
    monkeypatch.setenv("FAKE_ASSETS", "0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PEXELS_API_KEY", "fake-pexels-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "fake-pixabay-key")

    pexels_empty = MagicMock()
    pexels_empty.raise_for_status = MagicMock()
    pexels_empty.json.return_value = {"videos": []}

    pixabay_resp = MagicMock()
    pixabay_resp.raise_for_status = MagicMock()
    pixabay_resp.json.return_value = {
        "hits": [
            {
                "videos": {
                    "medium": {"url": "https://example.com/pixabay_medium.mp4", "size": 1000},
                    "small": {"url": "https://example.com/pixabay_small.mp4", "size": 500},
                }
            }
        ]
    }

    call_count = {"n": 0}
    downloaded_urls = []

    import httpx

    def fake_get(url, **kwargs):
        call_count["n"] += 1
        if "pexels" in url:
            return pexels_empty
        return pixabay_resp

    def fake_stream(method, url, **kwargs):
        downloaded_urls.append(url)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.raise_for_status = MagicMock()
        ctx.iter_bytes = MagicMock(return_value=[b"FAKE_VIDEO"])
        return ctx

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "stream", fake_stream)

    from app.pipeline.assets import fetch_scene_assets

    paths = fetch_scene_assets(
        content_id=11,
        scenes=[{"text": "Сцена 1", "keywords": ["city", "urban"]}],
        template="video_footage",
    )

    assert len(paths) == 1
    assert paths[0].suffix == ".mp4"
    assert any("pixabay" in u for u in downloaded_urls), "Скачивание с Pixabay не произошло"


def test_pexels_pixabay_both_empty_fallback_pollinations(tmp_path, monkeypatch):
    """Pexels пуст + Pixabay пуст + template=video_footage → фоллбэк на картинку Pollinations."""
    monkeypatch.setenv("FAKE_ASSETS", "0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PEXELS_API_KEY", "fake-pexels-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "fake-pixabay-key")

    empty_resp = MagicMock()
    empty_resp.raise_for_status = MagicMock()
    empty_resp.json.return_value = {"videos": [], "hits": []}

    downloaded_urls = []

    import httpx

    def fake_stream(method, url, **kwargs):
        downloaded_urls.append(url)
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=ctx)
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.raise_for_status = MagicMock()
        ctx.iter_bytes = MagicMock(return_value=[b"\xff\xd8\xff\xe0" + b"\x00" * 100])  # fake jpg
        return ctx

    monkeypatch.setattr(httpx, "get", lambda url, **kw: empty_resp)
    monkeypatch.setattr(httpx, "stream", fake_stream)

    from app.pipeline.assets import fetch_scene_assets

    paths = fetch_scene_assets(
        content_id=12,
        scenes=[{"text": "Сцена", "keywords": ["abstract", "minimal"]}],
        template="video_footage",
    )

    assert len(paths) == 1
    assert paths[0].suffix == ".jpg", f"Ожидался .jpg (фоллбэк), получен {paths[0].suffix}"
    assert any("pollinations" in u for u in downloaded_urls), (
        f"Pollinations не вызван. URLs: {downloaded_urls}"
    )


# ─── pick_music ───────────────────────────────────────────────────────────────


def test_pick_music_from_mood_dir(tmp_path, monkeypatch):
    """pick_music возвращает файл из папки настроения."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    mood_dir = tmp_path / "music" / "calm"
    mood_dir.mkdir(parents=True)
    mp3 = mood_dir / "calm_01.mp3"
    mp3.write_bytes(b"ID3")  # fake mp3

    from app.pipeline.assets import pick_music

    result = pick_music("calm")
    assert result is not None
    assert result == mp3


def test_pick_music_fallback_root(tmp_path, monkeypatch):
    """Папка настроения пуста → берёт из корня music/."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    music_root = tmp_path / "music"
    music_root.mkdir(parents=True)
    # Пустая папка настроения
    (music_root / "energetic").mkdir()
    # Файл в корне
    root_mp3 = music_root / "background.mp3"
    root_mp3.write_bytes(b"ID3")

    from app.pipeline.assets import pick_music

    result = pick_music("energetic")
    assert result is not None
    assert result == root_mp3


def test_pick_music_returns_none_when_empty(tmp_path, monkeypatch):
    """Нет mp3 нигде → None."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    # Создаём пустые папки
    (tmp_path / "music" / "neutral").mkdir(parents=True)

    from app.pipeline.assets import pick_music

    result = pick_music("neutral")
    assert result is None


def test_pick_music_no_music_dir(tmp_path, monkeypatch):
    """Директория music/ не существует → None."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.assets import pick_music

    result = pick_music("calm")
    assert result is None


# ─── rotate_media ─────────────────────────────────────────────────────────────


def _insert_test_content(db_path: Path, files: dict | None = None) -> int:
    """Вставить тестовый контент и вернуть его id."""
    files_json = json.dumps(files) if files else None
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        cur = conn.execute(
            "INSERT INTO projects (slug, name) VALUES (?, ?)",
            (f"proj-{id(db_path)}", "Тест"),
        )
        project_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO content (project_id, type, status, files) VALUES (?, 'video_footage', 'approved', ?)",
            (project_id, files_json),
        )
        content_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    return content_id


def _insert_schedule_row(db_path: Path, content_id: int, status: str, days_ago: int = 0) -> None:
    """Вставить запись schedule с нужным статусом и updated_at."""
    conn = sqlite3.connect(str(db_path))
    try:
        if days_ago > 0:
            updated_at = f"datetime('now', '-{days_ago} days')"
        else:
            updated_at = "datetime('now')"

        conn.execute(
            f"""
            INSERT INTO schedule (content_id, platform, planned_at, status, updated_at)
            VALUES (?, 'telegram', datetime('now'), ?, {updated_at})
            """,
            (content_id, status),
        )
        conn.commit()
    finally:
        conn.close()


def test_rotate_media_purges_old_published(tmp_path, monkeypatch):
    """
    Контент: все schedule published, updated_at 30 дней назад →
    файлы удалены, files.purged=True.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "14")

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    _setup_db(db_path)
    content_id = _insert_test_content(db_path)
    _insert_schedule_row(db_path, content_id, "published", days_ago=30)

    # Создаём файлы которые должны быть удалены
    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    (videos_dir / f"{content_id}.mp4").write_bytes(b"FAKE")
    (videos_dir / f"{content_id}.jpg").write_bytes(b"FAKE")

    media_dir = tmp_path / "media" / str(content_id)
    media_dir.mkdir(parents=True)
    (media_dir / "audio.wav").write_bytes(b"FAKE")

    from app.services.cleanup import rotate_media

    result = rotate_media()

    assert result["purged_content"] == 1
    assert not (videos_dir / f"{content_id}.mp4").exists(), "mp4 не удалён"
    assert not (videos_dir / f"{content_id}.jpg").exists(), "jpg не удалён"
    assert not media_dir.exists(), "media-каталог не удалён"

    # Проверяем files.purged=True
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT files FROM content WHERE id = ?", (content_id,)).fetchone()
    conn.close()
    files = json.loads(row[0])
    assert files.get("purged") is True


def test_rotate_media_keeps_recent_published(tmp_path, monkeypatch):
    """
    Контент published 3 дня назад при RETENTION=14 → НЕ удаляется.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "14")

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    _setup_db(db_path)
    content_id = _insert_test_content(db_path)
    _insert_schedule_row(db_path, content_id, "published", days_ago=3)

    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    mp4 = videos_dir / f"{content_id}.mp4"
    mp4.write_bytes(b"FAKE")

    from app.services.cleanup import rotate_media

    result = rotate_media()

    assert result["purged_content"] == 0
    assert mp4.exists(), "Свежий файл был удалён — ошибка!"


def test_rotate_media_orphan_dirs_removed(tmp_path, monkeypatch):
    """
    Осиротевшая media/{id}/ (нет в content) → удаляется.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "14")

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    _setup_db(db_path)

    # Создаём осиротевшую директорию (id=9999 не существует в content)
    orphan = tmp_path / "media" / "9999"
    orphan.mkdir(parents=True)
    (orphan / "some_file.wav").write_bytes(b"FAKE")

    from app.services.cleanup import rotate_media

    result = rotate_media()

    assert result["orphan_dirs"] == 1
    assert not orphan.exists(), "Осиротевшая директория не удалена"


def test_rotate_media_mixed_statuses_not_purged(tmp_path, monkeypatch):
    """
    Контент с одной published и одной planned schedule → НЕ удаляется.
    """
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "1")

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))

    _setup_db(db_path)
    content_id = _insert_test_content(db_path)
    _insert_schedule_row(db_path, content_id, "published", days_ago=30)
    _insert_schedule_row(db_path, content_id, "planned", days_ago=0)

    videos_dir = tmp_path / "videos"
    videos_dir.mkdir()
    mp4 = videos_dir / f"{content_id}.mp4"
    mp4.write_bytes(b"FAKE")

    from app.services.cleanup import rotate_media

    result = rotate_media()

    assert result["purged_content"] == 0
    assert mp4.exists(), "Контент с незавершённой schedule был удалён — ошибка!"


# ─── cleanup_after_render ─────────────────────────────────────────────────────


def test_cleanup_after_render_removes_assets(tmp_path, monkeypatch):
    """cleanup_after_render удаляет только assets/, оставляя остальное."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    media_dir = tmp_path / "media" / "5"
    assets_dir = media_dir / "assets"
    assets_dir.mkdir(parents=True)

    # Файлы в assets/ — должны быть удалены
    (assets_dir / "scene_01.mp4").write_bytes(b"FAKE")

    # Файлы вне assets/ — должны остаться
    audio = media_dir / "voice.wav"
    audio.write_bytes(b"FAKE_AUDIO")

    from app.services.cleanup import cleanup_after_render

    cleanup_after_render(5)

    assert not assets_dir.exists(), "assets/ не удалена"
    assert audio.exists(), "voice.wav был удалён — ошибка!"


def test_cleanup_after_render_no_assets_dir(tmp_path, monkeypatch):
    """cleanup_after_render не падает если assets/ не существует."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.services.cleanup import cleanup_after_render

    # Не должно поднимать исключений
    cleanup_after_render(999)
