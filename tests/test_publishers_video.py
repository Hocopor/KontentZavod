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


# ─── Тест 4: реальный путь VK видео — video.save вызван с user_token ─────────


def test_real_vk_video_success(tmp_path, monkeypatch):
    """
    Реальный режим VK + видео: video.save вызывается с user_token (не access_token).
    Итоговый URL содержит https://vk.com/video{owner_id}_{video_id}.
    """
    import httpx
    from app.publishers.vk import publish_vk

    files = _make_video_files(tmp_path)

    call_count = [0]
    video_save_params: dict = {}

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
            # Первый вызов — video.save: запоминаем параметры для проверки
            video_save_params.update(kwargs.get("params", {}))
            return FakeVideoSaveResp()
        else:
            # Второй вызов — upload на upload_url
            return FakeUploadResp()

    monkeypatch.setattr(httpx, "post", fake_post)

    url = publish_vk(
        schedule_id=4,
        credentials={"access_token": "group_token_vk", "user_token": "user_token_vk"},
        config={"group_id": 99999},
        texts=_make_texts(),
        files=files,
        dry_run=False,
    )

    assert url == "https://vk.com/video-99999_12345", f"Неожиданный URL: {url}"
    assert call_count[0] == 2, f"Ожидалось 2 вызова httpx.post, было {call_count[0]}"
    # video.save должен быть вызван с user_token, а не с групповым access_token
    assert video_save_params.get("access_token") == "user_token_vk", (
        f"video.save вызван не с user_token: {video_save_params.get('access_token')}"
    )


# ─── Тест 4b: wall.post (текстовый) использует групповой access_token ─────────


def test_real_vk_wall_post_uses_group_token(monkeypatch):
    """Текстовый пост (без видео и фото) — wall.post с групповым access_token."""
    import httpx
    from app.publishers.vk import publish_vk

    wall_params: dict = {}

    class FakeWallResp:
        def json(self):
            return {"response": {"post_id": 777}}

    def fake_post(url, *args, **kwargs):
        wall_params.update(kwargs.get("params", {}))
        return FakeWallResp()

    monkeypatch.setattr(httpx, "post", fake_post)

    url = publish_vk(
        schedule_id=41,
        credentials={"access_token": "group_token", "user_token": "user_token"},
        config={"group_id": 99999},
        texts=_make_texts(),
        files={},
        dry_run=False,
    )

    assert "wall-99999_777" in url
    # wall.post должен использовать групповой токен
    assert wall_params.get("access_token") == "group_token", (
        f"wall.post вызван не с групповым токеном: {wall_params.get('access_token')}"
    )


# ─── Тест 5: user_token отсутствует → PublishError с подсказкой про ошибку 27 ─


def test_real_vk_video_no_user_token(tmp_path):
    """Видео без user_token → PublishError с упоминанием ошибки 27."""
    from app.publishers.vk import publish_vk
    from app.publishers.base import PublishError

    files = _make_video_files(tmp_path)

    with pytest.raises(PublishError, match="ошибка 27"):
        publish_vk(
            schedule_id=5,
            credentials={"access_token": "group_only_token"},
            config={"group_id": 99999},
            texts=_make_texts(),
            files=files,
            dry_run=False,
        )


def test_real_vk_video_empty_credentials_no_user_token(tmp_path):
    """Видео с credentials=None → PublishError с подсказкой про user_token."""
    from app.publishers.vk import publish_vk
    from app.publishers.base import PublishError

    files = _make_video_files(tmp_path)

    with pytest.raises(PublishError, match="user_token"):
        publish_vk(
            schedule_id=51,
            credentials=None,
            config={"group_id": 99999},
            texts=_make_texts(),
            files=files,
            dry_run=False,
        )


# ─── Тест 5c: ошибка VK API при video.save → PublishError ─────────────────────


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
            credentials={"access_token": "group_token", "user_token": "user_token_vk"},
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


# ─── Тест 9: dry-run VK с image_path (пост с фото) ───────────────────────────


def test_dry_run_vk_post_with_image(tmp_path):
    """Dry-run VK с image_path (нет video_path) → outbox с method=wall.post+photo."""
    from app.publishers.vk import publish_vk
    import app.config as cfg_module

    # Создаём временный файл-картинку
    img_file = tmp_path / "post.jpg"
    img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)  # JPEG-заголовок

    url = publish_vk(
        schedule_id=9,
        credentials=None,
        config={"group_id": 99999},
        texts=_make_texts(),
        files={"image_path": str(img_file)},
        dry_run=True,
    )

    assert url == "dry-run://vk/9", f"Неожиданный URL: {url}"
    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / "9_vk.json"
    assert outbox_path.exists()
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["method"] == "wall.post+photo"
    assert payload["image_path"] == str(img_file)


# ─── Тест 10: dry-run Telegram с image_path (пост с фото) ────────────────────


def test_dry_run_telegram_post_with_image(tmp_path):
    """Dry-run Telegram с image_path (нет video_path) → method=sendPhoto."""
    from app.publishers.telegram import publish_telegram
    import app.config as cfg_module

    img_file = tmp_path / "post.jpg"
    img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

    url = publish_telegram(
        schedule_id=10,
        credentials=None,
        config={"channel_id": "@test_channel"},
        texts=_make_texts(),
        files={"image_path": str(img_file)},
        dry_run=True,
    )

    assert url == "dry-run://telegram/10", f"Неожиданный URL: {url}"
    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / "10_telegram.json"
    assert outbox_path.exists()
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert payload["method"] == "sendPhoto"
    assert payload["photo_path"] == str(img_file)
    assert payload["dry_run"] is True


# ─── Тест 11: real Telegram sendPhoto (короткий текст — caption) ─────────────


def test_real_telegram_photo_short_caption(tmp_path, monkeypatch):
    """Реальный Telegram с image_path + короткий текст → sendPhoto с caption."""
    import app.services.proxies as proxies_mod
    from app.publishers.telegram import publish_telegram

    img_file = tmp_path / "post.jpg"
    img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

    calls = []

    class FakeResp:
        def json(self):
            return {"ok": True, "result": {"message_id": 42}}

    class FakeClient:
        def __init__(self, proxy=None, timeout=None):
            pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, method, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return FakeResp()

    monkeypatch.setattr(proxies_mod.httpx, "Client", FakeClient)

    url = publish_telegram(
        schedule_id=11,
        credentials={"bot_token": "fake:TOKEN"},
        config={"channel_id": "@test_chan"},
        texts={"telegram": {"text": "Короткий текст", "hashtags": []}},
        files={"image_path": str(img_file)},
        dry_run=False,
    )

    assert url == "https://t.me/test_chan/42"
    assert len(calls) == 1
    assert "sendPhoto" in calls[0]["url"]
    # caption должен быть передан
    assert "caption" in calls[0].get("data", {})


# ─── Тест 12: real Telegram sendPhoto + длинный текст → 2 запроса ─────────────


def test_real_telegram_photo_long_text_sends_separately(tmp_path, monkeypatch):
    """Реальный Telegram + image_path + текст >1024 симв. → sendPhoto + sendMessage."""
    import app.services.proxies as proxies_mod
    from app.publishers.telegram import publish_telegram

    img_file = tmp_path / "post.jpg"
    img_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 20)

    calls = []

    class FakeResp:
        def json(self):
            return {"ok": True, "result": {"message_id": 55}}

    class FakeClient:
        def __init__(self, proxy=None, timeout=None):
            pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def request(self, method, url, **kwargs):
            calls.append(url)
            return FakeResp()

    monkeypatch.setattr(proxies_mod.httpx, "Client", FakeClient)

    long_text = "А" * 1100  # >1024

    url = publish_telegram(
        schedule_id=12,
        credentials={"bot_token": "fake:TOKEN"},
        config={"channel_id": "@test_chan"},
        texts={"telegram": {"text": long_text, "hashtags": []}},
        files={"image_path": str(img_file)},
        dry_run=False,
    )

    # Должно быть 2 запроса: sendPhoto + sendMessage
    assert len(calls) == 2, f"Ожидалось 2 вызова, было {len(calls)}: {calls}"
    assert any("sendPhoto" in c for c in calls)
    assert any("sendMessage" in c for c in calls)
    # URL по последнему сообщению (sendMessage)
    assert url == "https://t.me/test_chan/55"


# ─── Тест 13: md_to_telegram_html применяется при публикации ─────────────────


def test_telegram_text_converted_from_markdown(tmp_path):
    """Dry-run Telegram: markdown в тексте конвертируется → text в outbox содержит <b>."""
    from app.publishers.telegram import publish_telegram
    import app.config as cfg_module

    url = publish_telegram(
        schedule_id=13,
        credentials=None,
        config={"channel_id": "@test_channel"},
        texts={"telegram": {"text": "**Жирный** и *курсив*", "hashtags": []}},
        files={},
        dry_run=True,
    )

    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / "13_telegram.json"
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert "<b>Жирный</b>" in payload["text"]
    assert "<i>курсив</i>" in payload["text"]


# ─── Тест 14: md_to_plain применяется в VK при публикации ────────────────────


def test_vk_text_stripped_of_markdown():
    """Dry-run VK: markdown в тексте убирается → в outbox нет звёздочек."""
    from app.publishers.vk import publish_vk
    import app.config as cfg_module

    url = publish_vk(
        schedule_id=14,
        credentials=None,
        config={"group_id": 99999},
        texts={"vk": {"text": "**Жирный** и *курсив*", "hashtags": []}},
        files={},
        dry_run=True,
    )

    outbox_path = cfg_module.settings.data_dir_absolute / "outbox" / "14_vk.json"
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert "**" not in payload["message"]
    assert "*" not in payload["message"].replace("*", "")  # без звёздочек
    assert "Жирный" in payload["message"]
    assert "курсив" in payload["message"]
