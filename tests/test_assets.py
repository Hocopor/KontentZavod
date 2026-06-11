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


def test_pexels_pixabay_both_empty_fallback_fetch_image(tmp_path, monkeypatch):
    """Pexels видео пуст + Pixabay видео пуст + template=video_footage → фоллбэк через fetch_image."""
    monkeypatch.setenv("FAKE_ASSETS", "0")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PEXELS_API_KEY", "fake-pexels-key")
    monkeypatch.setenv("PIXABAY_API_KEY", "fake-pixabay-key")

    empty_resp = MagicMock()
    empty_resp.raise_for_status = MagicMock()
    empty_resp.json.return_value = {"videos": [], "hits": []}

    import app.pipeline.assets as assets_mod
    import httpx

    monkeypatch.setattr(httpx, "get", lambda url, **kw: empty_resp)

    # Мокаем fetch_image — единую цепочку картинок
    fetch_image_called = []

    def mock_fetch_image(keywords, dest):
        fetch_image_called.append(keywords)
        dest.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # fake jpg
        return True

    monkeypatch.setattr(assets_mod, "fetch_image", mock_fetch_image)

    from app.pipeline.assets import fetch_scene_assets

    paths = fetch_scene_assets(
        content_id=12,
        scenes=[{"text": "Сцена", "keywords": ["abstract", "minimal"]}],
        template="video_footage",
    )

    assert len(paths) == 1
    assert paths[0].suffix == ".jpg", f"Ожидался .jpg (фоллбэк), получен {paths[0].suffix}"
    assert len(fetch_image_called) == 1, "fetch_image должен вызываться как фоллбэк"


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


# ─── Прокси-фейловер ──────────────────────────────────────────────────────────


def _make_stream_ctx(data: bytes = b"FAKE") -> MagicMock:
    """Собрать fake context-manager для httpx.stream / client.stream."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.raise_for_status = MagicMock()
    ctx.iter_bytes = MagicMock(return_value=[data])
    return ctx


class TestProxyFailover:
    """
    Тесты прокси-фейловера в _http_get и _download_stream.
    Мокаем app.pipeline.assets.httpx (не глобальный httpx).
    """

    def test_download_stream_403_falls_back_to_proxy(self, tmp_path, monkeypatch):
        """
        Прямое скачивание → 403 HTTPStatusError → качается через прокси.
        """
        monkeypatch.setenv("FAKE_ASSETS", "0")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))
        monkeypatch.setenv("PEXELS_API_KEY", "")
        monkeypatch.setenv("PIXABAY_API_KEY", "")

        import httpx as httpx_module

        # Прямой stream: симулируем 403
        direct_resp = MagicMock()
        direct_resp.status_code = 403
        direct_resp.raise_for_status = MagicMock(
            side_effect=httpx_module.HTTPStatusError(
                "403 Forbidden",
                request=MagicMock(),
                response=direct_resp,
            )
        )
        direct_ctx = MagicMock()
        direct_ctx.__enter__ = MagicMock(return_value=direct_resp)
        direct_ctx.__exit__ = MagicMock(return_value=False)

        # Через прокси-клиент: успех
        proxy_stream_ctx = _make_stream_ctx(b"\xff\xd8\xff" + b"\x00" * 50)

        proxy_client = MagicMock()
        proxy_client.__enter__ = MagicMock(return_value=proxy_client)
        proxy_client.__exit__ = MagicMock(return_value=False)
        proxy_client.stream = MagicMock(return_value=proxy_stream_ctx)

        proxy_used = []

        class FakeClient:
            def __init__(self, proxy=None, timeout=None, **kw):
                self._proxy = proxy
                proxy_used.append(proxy)

            def __enter__(self):
                return proxy_client

            def __exit__(self, *args):
                pass

        # Мокаем _get_pool_proxies — один http-прокси
        monkeypatch.setattr(
            "app.pipeline.assets._get_pool_proxies",
            lambda: ["http://proxy.example.com:3128/"],
        )

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "stream", lambda *a, **kw: direct_ctx)
        monkeypatch.setattr(assets_mod.httpx, "Client", FakeClient)

        dest = tmp_path / "test.jpg"
        assets_mod._download_stream("https://image.pollinations.ai/test", dest)

        assert dest.exists(), "Файл должен быть скачан через прокси"
        assert len(proxy_used) >= 1, "Прокси-клиент должен быть создан"

    def test_download_stream_no_proxies_raises(self, tmp_path, monkeypatch):
        """
        Прямое скачивание → 403, прокси в пуле нет → raise HTTPStatusError.
        """
        import httpx as httpx_module

        direct_resp = MagicMock()
        direct_resp.status_code = 403
        direct_resp.raise_for_status = MagicMock(
            side_effect=httpx_module.HTTPStatusError(
                "403",
                request=MagicMock(),
                response=direct_resp,
            )
        )
        direct_ctx = MagicMock()
        direct_ctx.__enter__ = MagicMock(return_value=direct_resp)
        direct_ctx.__exit__ = MagicMock(return_value=False)

        monkeypatch.setattr(
            "app.pipeline.assets._get_pool_proxies",
            lambda: [],
        )

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "stream", lambda *a, **kw: direct_ctx)

        dest = tmp_path / "nope.jpg"
        with pytest.raises(httpx_module.HTTPStatusError):
            assets_mod._download_stream("https://example.com/video.mp4", dest)

    def test_download_stream_402_via_proxy_skips_to_next(self, tmp_path, monkeypatch):
        """
        Прямое скачивание → 403, первый прокси отвечает 402 (тариф исчерпан) →
        переходим ко второму прокси, который успешно скачивает.
        """
        import httpx as httpx_module

        # Прямой stream: симулируем 403
        direct_resp = MagicMock()
        direct_resp.status_code = 403
        direct_resp.raise_for_status = MagicMock(
            side_effect=httpx_module.HTTPStatusError(
                "403 Forbidden",
                request=MagicMock(),
                response=direct_resp,
            )
        )
        direct_ctx = MagicMock()
        direct_ctx.__enter__ = MagicMock(return_value=direct_resp)
        direct_ctx.__exit__ = MagicMock(return_value=False)

        proxy_calls: list[str] = []

        # Первый прокси возвращает 402 (сдохший тариф)
        dead_resp = MagicMock()
        dead_resp.status_code = 402
        dead_resp.request = MagicMock()
        dead_ctx = MagicMock()
        dead_ctx.__enter__ = MagicMock(return_value=dead_resp)
        dead_ctx.__exit__ = MagicMock(return_value=False)

        # Второй прокси — успех
        good_resp = MagicMock()
        good_resp.status_code = 200
        good_resp.request = MagicMock()
        good_resp.iter_bytes = MagicMock(return_value=[b"\xff\xd8\xff" + b"\x00" * 10])
        good_resp.raise_for_status = MagicMock()
        good_ctx = MagicMock()
        good_ctx.__enter__ = MagicMock(return_value=good_resp)
        good_ctx.__exit__ = MagicMock(return_value=False)

        proxy_stream_results = [dead_ctx, good_ctx]

        class FakeProxyClient:
            def __init__(self, proxy=None, timeout=None, **kw):
                self._proxy = proxy
                proxy_calls.append(proxy)
                self._idx = len(proxy_calls) - 1

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def stream(self, method, url, **kw):
                return proxy_stream_results[self._idx]

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "stream", lambda *a, **kw: direct_ctx)
        monkeypatch.setattr(assets_mod.httpx, "Client", FakeProxyClient)
        monkeypatch.setattr(
            "app.pipeline.assets._get_pool_proxies",
            lambda: ["http://dead-proxy:3128/", "http://good-proxy:3128/"],
        )

        dest = tmp_path / "stream402.jpg"
        assets_mod._download_stream("https://example.com/image.jpg", dest)

        assert dest.exists(), "Файл должен быть скачан через второй прокси"
        assert len(proxy_calls) == 2, f"Ожидался вызов двух прокси, было: {proxy_calls}"


# ─── Pexels Photos / Pixabay Photos ──────────────────────────────────────────


def _make_pexels_photo_response(portrait_url: str = "https://photos.pexels.com/portrait.jpg") -> MagicMock:
    """Собрать fake-ответ Pexels Photos API."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "photos": [
            {
                "id": 101,
                "src": {
                    "portrait": portrait_url,
                    "large2x": "https://photos.pexels.com/large2x.jpg",
                },
            }
        ]
    }
    return resp


def _make_pixabay_photo_response(image_url: str = "https://pixabay.com/photo.jpg") -> MagicMock:
    """Собрать fake-ответ Pixabay Photos API."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "hits": [
            {
                "id": 202,
                "largeImageURL": image_url,
                "webformatURL": "https://pixabay.com/small.jpg",
            }
        ]
    }
    return resp


class TestPexelsPhoto:
    """Тесты _fetch_pexels_photo."""

    def test_pexels_photo_success(self, tmp_path, monkeypatch):
        """Pexels Photos: портретный src.portrait → скачивается файл, возвращает True."""
        monkeypatch.setenv("PEXELS_API_KEY", "fake-pexels-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        pexels_resp = _make_pexels_photo_response()
        downloaded_urls = []

        def fake_stream(method, url, **kwargs):
            downloaded_urls.append(url)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.raise_for_status = MagicMock()
            ctx.iter_bytes = MagicMock(return_value=[b"\xff\xd8\xff" + b"\x00" * 20])
            return ctx

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", lambda url, **kw: pexels_resp)
        monkeypatch.setattr(assets_mod.httpx, "stream", fake_stream)

        dest = tmp_path / "photo.jpg"
        result = assets_mod._fetch_pexels_photo("marketing workspace", dest)

        assert result is True
        assert dest.exists()
        assert any("portrait" in u for u in downloaded_urls), (
            f"Должен быть скачан portrait URL, URLs: {downloaded_urls}"
        )

    def test_pexels_photo_no_key_returns_false(self, tmp_path, monkeypatch):
        """Без PEXELS_API_KEY → сразу False, без сетевых запросов."""
        monkeypatch.setenv("PEXELS_API_KEY", "")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod

        dest = tmp_path / "photo.jpg"
        result = assets_mod._fetch_pexels_photo("nature", dest)

        assert result is False
        assert not dest.exists()

    def test_pexels_photo_empty_result_returns_false(self, tmp_path, monkeypatch):
        """Pexels Photos возвращает пустой список → False."""
        monkeypatch.setenv("PEXELS_API_KEY", "fake-pexels-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        empty_resp = MagicMock()
        empty_resp.raise_for_status = MagicMock()
        empty_resp.json.return_value = {"photos": []}

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", lambda url, **kw: empty_resp)

        dest = tmp_path / "photo.jpg"
        result = assets_mod._fetch_pexels_photo("nonexistent query xyz", dest)

        assert result is False


class TestPixabayPhoto:
    """Тесты _fetch_pixabay_photo."""

    def test_pixabay_photo_success(self, tmp_path, monkeypatch):
        """Pixabay Photos: largeImageURL → скачивается файл, возвращает True."""
        monkeypatch.setenv("PIXABAY_API_KEY", "fake-pixabay-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        pixabay_resp = _make_pixabay_photo_response()
        downloaded_urls = []

        def fake_stream(method, url, **kwargs):
            downloaded_urls.append(url)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.raise_for_status = MagicMock()
            ctx.iter_bytes = MagicMock(return_value=[b"\xff\xd8\xff" + b"\x00" * 20])
            return ctx

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", lambda url, **kw: pixabay_resp)
        monkeypatch.setattr(assets_mod.httpx, "stream", fake_stream)

        dest = tmp_path / "pixabay_photo.jpg"
        result = assets_mod._fetch_pixabay_photo("business success", dest)

        assert result is True
        assert dest.exists()
        assert any("pixabay" in u for u in downloaded_urls), (
            f"Должен скачиваться URL от Pixabay, URLs: {downloaded_urls}"
        )

    def test_pixabay_photo_no_key_returns_false(self, tmp_path, monkeypatch):
        """Без PIXABAY_API_KEY → сразу False."""
        monkeypatch.setenv("PIXABAY_API_KEY", "")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod

        dest = tmp_path / "photo.jpg"
        result = assets_mod._fetch_pixabay_photo("nature", dest)

        assert result is False
        assert not dest.exists()

    def test_pixabay_photo_empty_result_returns_false(self, tmp_path, monkeypatch):
        """Pixabay Photos возвращает пустой список → False."""
        monkeypatch.setenv("PIXABAY_API_KEY", "fake-pixabay-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        empty_resp = MagicMock()
        empty_resp.raise_for_status = MagicMock()
        empty_resp.json.return_value = {"hits": []}

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", lambda url, **kw: empty_resp)

        dest = tmp_path / "photo.jpg"
        result = assets_mod._fetch_pixabay_photo("nonexistent xyz 999", dest)

        assert result is False


# ─── fetch_image цепочка ──────────────────────────────────────────────────────


class TestFetchImage:
    """Тесты публичной функции fetch_image."""

    def test_fake_assets_returns_true(self, tmp_path, monkeypatch):
        """FAKE_ASSETS=1 → _fake_image + True без сети."""
        monkeypatch.setenv("FAKE_ASSETS", "1")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Мокаем _fake_image чтобы не нужен был ffmpeg
        import app.pipeline.assets as assets_mod
        fake_called = []

        def mock_fake_image(dest):
            fake_called.append(dest)
            dest.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)

        monkeypatch.setattr(assets_mod, "_fake_image", mock_fake_image)

        dest = tmp_path / "result.jpg"
        result = assets_mod.fetch_image(["nature", "sunset"], dest)

        assert result is True
        assert len(fake_called) == 1

    def test_pexels_first_in_chain(self, tmp_path, monkeypatch):
        """Pexels — первый в цепочке, при успехе остальные не вызываются."""
        monkeypatch.setenv("FAKE_ASSETS", "0")
        monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod

        call_order = []

        def mock_pexels_photo(query, dest):
            call_order.append("pexels")
            dest.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
            return True

        def mock_pixabay_photo(query, dest):
            call_order.append("pixabay")
            return False

        def mock_openverse(query, dest):
            call_order.append("openverse")
            return False

        def mock_wikimedia(query, dest):
            call_order.append("wikimedia")
            return False

        monkeypatch.setattr(assets_mod, "_fetch_pexels_photo", mock_pexels_photo)
        monkeypatch.setattr(assets_mod, "_fetch_pixabay_photo", mock_pixabay_photo)
        monkeypatch.setattr(assets_mod, "_fetch_openverse_photo", mock_openverse)
        monkeypatch.setattr(assets_mod, "_fetch_wikimedia_photo", mock_wikimedia)

        dest = tmp_path / "result.jpg"
        result = assets_mod.fetch_image(["nature"], dest)

        assert result is True
        assert call_order == ["pexels"], f"Только Pexels должен быть вызван, порядок: {call_order}"

    def test_pexels_success_skips_pixabay(self, tmp_path, monkeypatch):
        """Pexels успешен → Pixabay не вызывается."""
        monkeypatch.setenv("FAKE_ASSETS", "0")
        monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
        monkeypatch.setenv("PIXABAY_API_KEY", "fake-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod

        pixabay_called = []

        def mock_pexels_photo(query, dest):
            dest.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
            return True

        def mock_pixabay_photo(query, dest):
            pixabay_called.append(query)
            return False

        monkeypatch.setattr(assets_mod, "_fetch_pexels_photo", mock_pexels_photo)
        monkeypatch.setattr(assets_mod, "_fetch_pixabay_photo", mock_pixabay_photo)

        dest = tmp_path / "result.jpg"
        result = assets_mod.fetch_image(["business"], dest)

        assert result is True
        assert len(pixabay_called) == 0, "Pixabay не должен вызываться если Pexels успешен"

    def test_chain_order_pexels_pixabay_openverse_wikimedia(self, tmp_path, monkeypatch):
        """Все источники вызываются по порядку: Pexels→Pixabay→Openverse→Wikimedia."""
        monkeypatch.setenv("FAKE_ASSETS", "0")
        monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
        monkeypatch.setenv("PIXABAY_API_KEY", "fake-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod

        call_order = []

        monkeypatch.setattr(assets_mod, "_fetch_pexels_photo", lambda q, d: call_order.append("pexels") or False)
        monkeypatch.setattr(assets_mod, "_fetch_pixabay_photo", lambda q, d: call_order.append("pixabay") or False)
        monkeypatch.setattr(assets_mod, "_fetch_openverse_photo", lambda q, d: call_order.append("openverse") or False)

        def mock_wikimedia(query, dest):
            call_order.append("wikimedia")
            dest.write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
            return True

        monkeypatch.setattr(assets_mod, "_fetch_wikimedia_photo", mock_wikimedia)

        dest = tmp_path / "result.jpg"
        result = assets_mod.fetch_image(["mountain", "lake"], dest)

        assert result is True
        assert call_order == ["pexels", "pixabay", "openverse", "wikimedia"], (
            f"Неверный порядок вызовов: {call_order}"
        )

    def test_all_sources_fail_returns_false(self, tmp_path, monkeypatch):
        """Все источники провалились → False."""
        monkeypatch.setenv("FAKE_ASSETS", "0")
        monkeypatch.setenv("PEXELS_API_KEY", "fake-key")
        monkeypatch.setenv("PIXABAY_API_KEY", "fake-key")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod

        monkeypatch.setattr(assets_mod, "_fetch_pexels_photo", lambda q, d: False)
        monkeypatch.setattr(assets_mod, "_fetch_pixabay_photo", lambda q, d: False)
        monkeypatch.setattr(assets_mod, "_fetch_openverse_photo", lambda q, d: False)
        monkeypatch.setattr(assets_mod, "_fetch_wikimedia_photo", lambda q, d: False)

        dest = tmp_path / "result.jpg"
        result = assets_mod.fetch_image(["some", "query"], dest)

        assert result is False
        assert not dest.exists()


# ─── Openverse Photos ─────────────────────────────────────────────────────────


def _make_openverse_response(
    results: list[dict] | None = None,
) -> MagicMock:
    """Собрать fake-ответ Openverse API."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"results": results or []}
    return resp


class TestOpenversePhoto:
    """Тесты _fetch_openverse_photo."""

    def test_openverse_success_vertical_preferred(self, tmp_path, monkeypatch):
        """Openverse: есть вертикальное фото → берём его, а не горизонтальное."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        results = [
            {"url": "https://openverse.org/horizontal.jpg", "width": 1920, "height": 1080},
            {"url": "https://openverse.org/vertical.jpg",   "width": 1080, "height": 1920},
        ]
        openverse_resp = _make_openverse_response(results)
        downloaded_urls = []

        def fake_stream(method, url, **kwargs):
            downloaded_urls.append(url)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.raise_for_status = MagicMock()
            ctx.iter_bytes = MagicMock(return_value=[b"\xff\xd8\xff" + b"\x00" * 20])
            return ctx

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", lambda url, **kw: openverse_resp)
        monkeypatch.setattr(assets_mod.httpx, "stream", fake_stream)

        dest = tmp_path / "openverse.jpg"
        result = assets_mod._fetch_openverse_photo("nature landscape", dest)

        assert result is True
        assert dest.exists()
        assert any("vertical" in u for u in downloaded_urls), (
            f"Должен быть выбран вертикальный, URLs: {downloaded_urls}"
        )

    def test_openverse_empty_results_returns_false(self, tmp_path, monkeypatch):
        """Openverse возвращает пустые results → False."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(
            assets_mod.httpx, "get",
            lambda url, **kw: _make_openverse_response([]),
        )

        dest = tmp_path / "openverse.jpg"
        result = assets_mod._fetch_openverse_photo("xyz 404 nothing", dest)

        assert result is False
        assert not dest.exists()

    def test_openverse_http_error_returns_false(self, tmp_path, monkeypatch):
        """Openverse возвращает HTTP-ошибку → warning + False."""
        import httpx as httpx_module
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(
            side_effect=httpx_module.HTTPStatusError(
                "500",
                request=MagicMock(),
                response=bad_resp,
            )
        )

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", lambda url, **kw: bad_resp)

        dest = tmp_path / "openverse.jpg"
        result = assets_mod._fetch_openverse_photo("error query", dest)

        assert result is False


# ─── Wikimedia Commons ────────────────────────────────────────────────────────


def _make_wikimedia_response(pages: dict | None = None) -> MagicMock:
    """Собрать fake-ответ Wikimedia API."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"query": {"pages": pages or {}}}
    return resp


class TestWikimediaPhoto:
    """Тесты _fetch_wikimedia_photo."""

    def test_wikimedia_success(self, tmp_path, monkeypatch):
        """Wikimedia: успешный запрос, скачивает thumburl."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        pages = {
            "1": {
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/orig.jpg",
                        "thumburl": "https://upload.wikimedia.org/thumb.jpg",
                        "width": 1080,
                        "height": 1920,
                    }
                ]
            }
        }
        wikimedia_resp = _make_wikimedia_response(pages)
        downloaded_urls = []

        def fake_stream(method, url, **kwargs):
            downloaded_urls.append(url)
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.raise_for_status = MagicMock()
            ctx.iter_bytes = MagicMock(return_value=[b"\xff\xd8\xff" + b"\x00" * 20])
            return ctx

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", lambda url, **kw: wikimedia_resp)
        monkeypatch.setattr(assets_mod.httpx, "stream", fake_stream)

        dest = tmp_path / "wikimedia.jpg"
        result = assets_mod._fetch_wikimedia_photo("mountain landscape", dest)

        assert result is True
        assert dest.exists()
        assert any("thumb" in u for u in downloaded_urls), (
            f"Должен скачиваться thumburl, URLs: {downloaded_urls}"
        )

    def test_wikimedia_no_pages_returns_false(self, tmp_path, monkeypatch):
        """Wikimedia: нет страниц в ответе → False."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(
            assets_mod.httpx, "get",
            lambda url, **kw: _make_wikimedia_response({}),
        )

        dest = tmp_path / "wikimedia.jpg"
        result = assets_mod._fetch_wikimedia_photo("nothing found xyz", dest)

        assert result is False
        assert not dest.exists()

    def test_wikimedia_user_agent_in_request(self, tmp_path, monkeypatch):
        """Wikimedia: User-Agent присутствует в запросе к API."""
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        captured_headers: list[dict] = []

        def fake_get(url, headers=None, params=None, **kw):
            captured_headers.append(dict(headers or {}))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"query": {"pages": {}}}
            return resp

        import app.pipeline.assets as assets_mod
        monkeypatch.setattr(assets_mod.httpx, "get", fake_get)

        dest = tmp_path / "wikimedia.jpg"
        assets_mod._fetch_wikimedia_photo("test query", dest)

        assert len(captured_headers) >= 1
        ua = captured_headers[0].get("User-Agent", "")
        assert "KontentZavod" in ua, (
            f"User-Agent должен содержать 'KontentZavod', получили: {ua!r}"
        )
