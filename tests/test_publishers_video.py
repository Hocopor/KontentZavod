"""
Тесты публикации ВИДЕО для паблишеров VK и Telegram.

pytest tests/test_publishers_video.py -q
"""
import json
from pathlib import Path

import pytest


# ─── Общие фикстуры ───────────────────────────────────────────────────────────


def _make_texts() -> dict:
    """Стандартный набор текстов для тестов."""
    return {
        "vk": {
            "text": "Тестовое видео для VK",
            "hashtags": ["#видео", "#тест"],
        },
        "telegram": {
            "text": "Тестовое видео для Telegram",
            "hashtags": ["#видео"],
        },
    }


def _make_video_files(tmp_path: Path) -> dict:
    """Создаёт временный mp4-файл и возвращает files-словарь."""
    video_file = tmp_path / "test_video.mp4"
    video_file.write_bytes(b"\x00\x01\x02\x03")  # мок mp4-данных
    return {
        "video_path": str(video_file),
        "preview_path": str(tmp_path / "preview.jpg"),
        "subs_path": str(tmp_path / "subs.srt"),
    }


# ─── Тест 1: dry-run VK видео → outbox с method=video.save ───────────────────


def test_dry_run_vk_video(tmp_path):
    """Dry-run VK с video_path → outbox содержит method=video.save, wallpost=1."""
    from app.publishers.vk import publish_vk
    import app.config as cfg_module

    files = _make_video_files(tmp_path)
    texts = _make_texts()
    config = {"group_id": 99999}

    url = publish_vk(
        schedule_id=1,
        credentials=None,
        config=config,
        texts=texts,
        files=files,
        dry_run=True,
    )

    assert url == "dry-run://vk/1", f"Неожиданный URL: {url}"

    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / "1_vk.json"
    assert outbox_path.exists(), f"outbox-файл не создан: {outbox_path}"

    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["method"] == "video.save"
    assert payload["wallpost"] == 1
    assert payload["group_id"] == 99999
    assert payload["video_path"] == files["video_path"]
    assert "name" in payload
    assert "description" in payload


# ─── Тест 2: dry-run VK без видео → старый wall.post ─────────────────────────


def test_dry_run_vk_no_video(tmp_path):
    """Dry-run VK без video_path → outbox содержит method=wall.post (старый путь)."""
    from app.publishers.vk import publish_vk
    import app.config as cfg_module

    url = publish_vk(
        schedule_id=2,
        credentials=None,
        config={"group_id": 99999},
        texts=_make_texts(),
        files={},
        dry_run=True,
    )

    assert url == "dry-run://vk/2", f"Неожиданный URL: {url}"

    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / "2_vk.json"
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["method"] == "wall.post"
    assert payload["dry_run"] is True


# ─── Тест 3: dry-run Telegram видео → method=sendVideo ────────────────────────


def test_dry_run_telegram_video(tmp_path):
    """Dry-run Telegram с video_path → outbox содержит method=sendVideo и video_path."""
    from app.publishers.telegram import publish_telegram
    import app.config as cfg_module

    files = _make_video_files(tmp_path)

    url = publish_telegram(
        schedule_id=3,
        credentials=None,
        config={"channel_id": "@test_channel"},
        texts=_make_texts(),
        files=files,
        dry_run=True,
    )

    assert url == "dry-run://telegram/3", f"Неожиданный URL: {url}"

    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / "3_telegram.json"
    assert outbox_path.exists(), f"outbox-файл не создан: {outbox_path}"

    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["method"] == "sendVideo"
    assert payload["video_path"] == files["video_path"]
    assert payload["dry_run"] is True


# ─── Тест 4: реальный путь VK видео (monkeypatch httpx.post) ─────────────────


def test_real_vk_video_success(tmp_path, monkeypatch):
    """
    Реальный режим VK + видео: последовательно мокаем video.save → upload.
    Итоговый URL содержит https://vk.com/video{owner_id}_{video_id}.
    """
    import httpx
    from app.publishers.vk import publish_vk

    files = _make_video_files(tmp_path)

    call_count = [0]

    class FakeVideoSaveResp:
        def json(self):
            return {
                "response": {
                    "upload_url": "https://upload.vk.com/fake_upload",
                    "owner_id": -99999,
                    "video_id": 12345,
                }
            }

    class FakeUploadResp:
        def json(self):
            return {"owner_id": -99999, "video_id": 12345}

    def fake_post(url, *args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            # Первый вызов — video.save
            return FakeVideoSaveResp()
        else:
            # Второй вызов — upload на upload_url
            return FakeUploadResp()

    monkeypatch.setattr(httpx, "post", fake_post)

    url = publish_vk(
        schedule_id=4,
        credentials={"access_token": "fake_token_vk"},
        config={"group_id": 99999},
        texts=_make_texts(),
        files=files,
        dry_run=False,
    )

    assert url == "https://vk.com/video-99999_12345", f"Неожиданный URL: {url}"
    assert call_count[0] == 2, f"Ожидалось 2 вызова httpx.post, было {call_count[0]}"


# ─── Тест 5: реальный путь VK видео — ошибка API video.save ──────────────────


def test_real_vk_video_api_error(tmp_path, monkeypatch):
    """Ошибка VK API при video.save → PublishError."""
    import httpx
    from app.publishers.vk import publish_vk
    from app.publishers.base import PublishError

    files = _make_video_files(tmp_path)

    class FakeErrorResp:
        def json(self):
            return {
                "error": {
                    "error_code": 15,
                    "error_msg": "Access denied: group hide members",
                }
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: FakeErrorResp())

    with pytest.raises(PublishError, match="video.save"):
        publish_vk(
            schedule_id=5,
            credentials={"access_token": "fake_token_vk"},
            config={"group_id": 99999},
            texts=_make_texts(),
            files=files,
            dry_run=False,
        )


# ─── Тест 6: реальный путь Telegram видео ─────────────────────────────────────


def test_real_telegram_video_success(tmp_path, monkeypatch):
    """Реальный режим Telegram + видео → URL t.me/..."""
    import app.services.proxies as proxies_mod
    from app.publishers.telegram import publish_telegram

    files = _make_video_files(tmp_path)

    class FakeResp:
        def json(self):
            return {"ok": True, "result": {"message_id": 777}}

    class FakeClient:
        def __init__(self, proxy=None, timeout=None):
            pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, method, url, **kwargs): return FakeResp()

    monkeypatch.setattr(proxies_mod.httpx, "Client", FakeClient)

    url = publish_telegram(
        schedule_id=6,
        credentials={"bot_token": "fake:TOKEN"},
        config={"channel_id": "@test_chan"},
        texts=_make_texts(),
        files=files,
        dry_run=False,
    )

    assert url == "https://t.me/test_chan/777", f"Неожиданный URL: {url}"


# ─── Тест 7: несуществующий video_path → PublishError (VK) ───────────────────


def test_real_vk_video_missing_file(tmp_path):
    """VK реальный режим: video_path указывает на несуществующий файл → PublishError."""
    from app.publishers.vk import publish_vk
    from app.publishers.base import PublishError

    files = {
        "video_path": str(tmp_path / "nonexistent.mp4"),
    }

    with pytest.raises(PublishError, match="видео-файл не найден"):
        publish_vk(
            schedule_id=7,
            credentials={"access_token": "fake_token_vk"},
            config={"group_id": 99999},
            texts=_make_texts(),
            files=files,
            dry_run=False,
        )


# ─── Тест 8: несуществующий video_path → PublishError (Telegram) ─────────────


def test_real_telegram_video_missing_file(tmp_path):
    """Telegram реальный режим: video_path указывает на несуществующий файл → PublishError."""
    from app.publishers.telegram import publish_telegram
    from app.publishers.base import PublishError

    files = {
        "video_path": str(tmp_path / "nonexistent.mp4"),
    }

    with pytest.raises(PublishError, match="видео-файл не найден"):
        publish_telegram(
            schedule_id=8,
            credentials={"bot_token": "fake:TOKEN"},
            config={"channel_id": "@test_chan"},
            texts=_make_texts(),
            files=files,
            dry_run=False,
        )
