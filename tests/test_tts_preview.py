"""
Тесты роута /tts/preview/{voice}.

Сценарии:
  - FAKE_TTS=1: 200 + Content-Type audio/mpeg + кеш-файл создан.
  - Повторный запрос: возвращает кеш (файл уже есть).
  - Неизвестный голос → 404.

pytest tests/test_tts_preview.py -q
"""
import shutil
from pathlib import Path

import pytest

# Проверка доступности ffmpeg (нужен для FAKE_TTS синтеза тишины)
_HAS_FFMPEG  = shutil.which("ffmpeg")  is not None
_HAS_FFPROBE = shutil.which("ffprobe") is not None
_NEEDS_FF = pytest.mark.skipif(
    not (_HAS_FFMPEG and _HAS_FFPROBE),
    reason="ffmpeg/ffprobe не найдены в PATH",
)


# ─── Тесты ────────────────────────────────────────────────────────────────────


class TestTtsPreview:
    @_NEEDS_FF
    def test_svetlana_returns_200_and_audio_mpeg(self, client, patch_env, monkeypatch):
        """GET /tts/preview/svetlana → 200, Content-Type: audio/mpeg."""
        monkeypatch.setenv("FAKE_TTS", "1")
        resp = client.get("/tts/preview/svetlana")
        assert resp.status_code == 200
        assert "audio/mpeg" in resp.headers.get("content-type", "")

    @_NEEDS_FF
    def test_dmitry_returns_200_and_audio_mpeg(self, client, patch_env, monkeypatch):
        """GET /tts/preview/dmitry → 200, Content-Type: audio/mpeg."""
        monkeypatch.setenv("FAKE_TTS", "1")
        resp = client.get("/tts/preview/dmitry")
        assert resp.status_code == 200
        assert "audio/mpeg" in resp.headers.get("content-type", "")

    @_NEEDS_FF
    def test_cache_file_created(self, client, patch_env, monkeypatch, tmp_path):
        """После запроса кеш-файл data/tts_preview/svetlana_p0pct_p0hz.mp3 создаётся."""
        monkeypatch.setenv("FAKE_TTS", "1")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        resp = client.get("/tts/preview/svetlana")
        assert resp.status_code == 200

        cache_path = tmp_path / "tts_preview" / "svetlana_p0pct_p0hz.mp3"
        assert cache_path.exists(), "Кеш-файл не создан"
        assert cache_path.stat().st_size > 0, "Кеш-файл пустой"

    @_NEEDS_FF
    def test_second_request_uses_cache(self, client, patch_env, monkeypatch, tmp_path):
        """Повторный запрос отдаёт кеш (без повторного синтеза)."""
        monkeypatch.setenv("FAKE_TTS", "1")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Первый запрос — создаёт кеш
        resp1 = client.get("/tts/preview/dmitry")
        assert resp1.status_code == 200

        cache_path = tmp_path / "tts_preview" / "dmitry_p0pct_p0hz.mp3"
        assert cache_path.exists()
        mtime_after_first = cache_path.stat().st_mtime

        # Второй запрос — кеш не должен измениться (файл не перезаписан)
        resp2 = client.get("/tts/preview/dmitry")
        assert resp2.status_code == 200
        assert cache_path.stat().st_mtime == mtime_after_first, "Кеш был перезаписан при повторном запросе"

    def test_unknown_voice_returns_404(self, client, patch_env):
        """Неизвестный голос → 404."""
        resp = client.get("/tts/preview/unknown_voice")
        assert resp.status_code == 404

    def test_empty_voice_returns_404(self, client, patch_env):
        """Голос 'hacker' → 404."""
        resp = client.get("/tts/preview/hacker")
        assert resp.status_code == 404

    @_NEEDS_FF
    def test_preview_with_rate_pitch_separate_cache(self, client, patch_env, monkeypatch, tmp_path):
        """
        GET /tts/preview/svetlana?rate=-10%&pitch=+8Hz → 200, audio/mpeg;
        кеш-файл имеет суффикс с m10pct и p8hz (не обычный p0pct_p0hz).
        """
        monkeypatch.setenv("FAKE_TTS", "1")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        resp = client.get("/tts/preview/svetlana?rate=-10%&pitch=%2B8Hz")
        assert resp.status_code == 200
        assert "audio/mpeg" in resp.headers.get("content-type", "")

        preview_dir = tmp_path / "tts_preview"
        files = list(preview_dir.glob("svetlana_*.mp3"))
        assert files, "Кеш-файл не создан"
        names = [f.name for f in files]
        assert any("m10pct" in n for n in names), f"Суффикс m10pct не найден в именах: {names}"
        assert any("p8hz"   in n for n in names), f"Суффикс p8hz не найден в именах: {names}"

    def test_preview_invalid_rate_falls_back(self, client, patch_env, monkeypatch, tmp_path):
        """
        GET /tts/preview/dmitry?rate=ВЗЛОМ&pitch=ХАК → 200 (валидаторы подставили дефолт).
        Тест без ffmpeg: кеш уже создан вручную.
        """
        monkeypatch.setenv("FAKE_TTS", "1")
        monkeypatch.setenv("DATA_DIR", str(tmp_path))

        # Создаём фиктивный кеш-файл для дефолтных параметров (dmitry_p0pct_p0hz.mp3)
        cache_dir = tmp_path / "tts_preview"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / "dmitry_p0pct_p0hz.mp3"
        cache_file.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 100)

        resp = client.get("/tts/preview/dmitry?rate=ВЗЛОМ&pitch=ХАК")
        # Валидаторы подставили дефолт → нашли кеш → 200
        assert resp.status_code == 200
