"""
Тесты харденинга Settings против systemd-мусора в значениях.

systemd (EnvironmentFile=) НЕ удаляет inline-комментарии в .env:
строка «KEY=значение  # пояснение» приходит в os.environ целиком.
Для однотокенных полей (URL, ключи) и bool/int Settings отсекает хвост.
"""
import app.config as cfg_module


def test_base_url_inline_comment_stripped(monkeypatch):
    monkeypatch.setenv(
        "LLM_ROUTER_BASE_URL",
        "https://router.mak-o.ru/v1/ # если LLMRouter на этом же сервере",
    )
    assert cfg_module.settings.LLM_ROUTER_BASE_URL == "https://router.mak-o.ru/v1/"


def test_key_inline_comment_stripped(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_KEY", "vk-abc123  # виртуальный ключ роутера")
    assert cfg_module.settings.LLM_ROUTER_KEY == "vk-abc123"


def test_bool_inline_comment_stripped(monkeypatch):
    monkeypatch.setenv("PUBLISH_DRY_RUN", "1 # до реальных токенов")
    assert cfg_module.settings.PUBLISH_DRY_RUN is True
    monkeypatch.setenv("PUBLISH_DRY_RUN", "0 # боевой режим")
    assert cfg_module.settings.PUBLISH_DRY_RUN is False


def test_int_inline_comment_stripped(monkeypatch):
    monkeypatch.setenv("YOUTUBE_DAILY_LIMIT", "6 # квота Data API")
    assert cfg_module.settings.YOUTUBE_DAILY_LIMIT == 6


def test_empty_values_safe(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_KEY", "")
    assert cfg_module.settings.LLM_ROUTER_KEY == ""
    monkeypatch.setenv("FAKE_TTS", "")
    assert cfg_module.settings.FAKE_TTS is False


def test_clean_values_untouched(monkeypatch):
    monkeypatch.setenv("LLM_ROUTER_BASE_URL", "http://127.0.0.1:8100/v1")
    assert cfg_module.settings.LLM_ROUTER_BASE_URL == "http://127.0.0.1:8100/v1"
    # Путь к ffmpeg может содержать пробелы — не трогаем
    monkeypatch.setenv("FFMPEG_BIN", r"C:\Program Files\FFmpeg\bin\ffmpeg.exe")
    assert cfg_module.settings.FFMPEG_BIN == r"C:\Program Files\FFmpeg\bin\ffmpeg.exe"
