"""
Тесты модулей tts.py и subtitles.py (этап 3.1).

Запуск:
    pytest tests/test_tts.py -q

Все тесты работают с FAKE_TTS=1 (без сети).
ffmpeg/ffprobe-зависимые тесты пропускаются, если бинари недоступны.
"""
import asyncio
import math
import re
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ─── Вспомогательное ────────────────────────────────────────────────────────

_HAS_FFMPEG  = shutil.which("ffmpeg")  is not None
_HAS_FFPROBE = shutil.which("ffprobe") is not None
_NEEDS_FF = pytest.mark.skipif(
    not (_HAS_FFMPEG and _HAS_FFPROBE),
    reason="ffmpeg/ffprobe не найдены в PATH",
)

_SCENES = [
    "Привет мир тест",       # 3 слова
    "Второй текст сцены здесь",  # 4 слова
]


# ─── Тесты synthesize_scenes ────────────────────────────────────────────────


@_NEEDS_FF
def test_files_created(tmp_path, monkeypatch):
    """Файлы voice_01.mp3, voice_02.mp3 должны создаться."""
    monkeypatch.setenv("FAKE_TTS", "1")
    from app.pipeline.tts import synthesize_scenes

    out = tmp_path / "audio"
    result = synthesize_scenes(_SCENES, out)

    assert (out / "voice_01.mp3").exists(), "voice_01.mp3 не создан"
    assert (out / "voice_02.mp3").exists(), "voice_02.mp3 не создан"
    assert len(result) == len(_SCENES)


@_NEEDS_FF
def test_duration_positive(tmp_path, monkeypatch):
    """duration каждой сцены должна быть > 0."""
    monkeypatch.setenv("FAKE_TTS", "1")
    from app.pipeline.tts import synthesize_scenes

    result = synthesize_scenes(_SCENES, tmp_path / "audio")
    for sa in result:
        assert sa.duration > 0, f"Сцена {sa.index}: duration <= 0"


@_NEEDS_FF
def test_word_count_matches(tmp_path, monkeypatch):
    """Число WordTiming должно совпадать с числом слов в тексте."""
    monkeypatch.setenv("FAKE_TTS", "1")
    from app.pipeline.tts import synthesize_scenes

    result = synthesize_scenes(_SCENES, tmp_path / "audio")
    for sa, text in zip(result, _SCENES):
        expected = len(text.split())
        assert len(sa.words) == expected, (
            f"Сцена {sa.index}: ожидалось {expected} слов, получено {len(sa.words)}"
        )


@_NEEDS_FF
def test_global_timings_monotonic(tmp_path, monkeypatch):
    """
    Тайминги слов должны быть монотонно возрастающими сквозь все сцены
    (глобальная шкала).
    """
    monkeypatch.setenv("FAKE_TTS", "1")
    from app.pipeline.tts import synthesize_scenes

    result = synthesize_scenes(_SCENES, tmp_path / "audio")

    # Собираем все start-тайминги сквозь все сцены
    all_starts = [wt.start for sa in result for wt in sa.words]
    assert all_starts == sorted(all_starts), (
        "Глобальные start-тайминги не монотонны"
    )
    # Слова второй сцены должны стартовать позже конца первой сцены
    end_of_scene_0 = result[0].duration
    starts_scene_1 = [wt.start for wt in result[1].words]
    assert all(s >= end_of_scene_0 for s in starts_scene_1), (
        "Тайминги второй сцены не учитывают длительность первой"
    )


@_NEEDS_FF
def test_global_offset_equals_sum_of_durations(tmp_path, monkeypatch):
    """
    Первое слово второй сцены должно стартовать ровно через duration первой.
    """
    monkeypatch.setenv("FAKE_TTS", "1")
    from app.pipeline.tts import synthesize_scenes

    result = synthesize_scenes(_SCENES, tmp_path / "audio")
    offset = result[0].duration
    first_word_scene1 = result[1].words[0]
    assert abs(first_word_scene1.start - offset) < 1e-6, (
        f"Ожидали start={offset:.4f}, получили {first_word_scene1.start:.4f}"
    )


@_NEEDS_FF
def test_out_dir_created_automatically(tmp_path, monkeypatch):
    """out_dir создаётся автоматически (mkdir parents)."""
    monkeypatch.setenv("FAKE_TTS", "1")
    from app.pipeline.tts import synthesize_scenes

    deep_dir = tmp_path / "a" / "b" / "c"
    assert not deep_dir.exists()
    synthesize_scenes(["Тест"], deep_dir)
    assert deep_dir.exists()


def test_unknown_voice_raises(tmp_path, monkeypatch):
    """Неизвестный голос → TTSError (без сети, без ffmpeg)."""
    monkeypatch.setenv("FAKE_TTS", "1")
    from app.pipeline.tts import synthesize_scenes, TTSError

    with pytest.raises(TTSError, match="Неизвестный голос"):
        synthesize_scenes(["Привет"], tmp_path, voice="unknown_voice")


# ─── Тесты proxy-фейловера ───────────────────────────────────────────────────


def _make_fake_audio_chunks():
    """Минимальный async-генератор, имитирующий успешный стрим от edge-tts."""
    async def _gen():
        # Минимальный валидный MP3-заголовок (ID3) — тесту хватит
        yield {"type": "audio", "data": b"\xff\xfb\x90\x00" + b"\x00" * 413}
        yield {
            "type": "WordBoundary",
            "offset": 0,
            "duration": 5_000_000,
            "text": "Привет",
        }
    return _gen()


def test_proxy_fallback_on_direct_failure(tmp_path, monkeypatch):
    """
    При провале прямой попытки edge-tts должен уйти через прокси.

    Мокаем:
      - edge_tts.Communicate: первый вызов (proxy=None) бросает RuntimeError,
        второй (с proxy) возвращает успешный стрим.
      - app.pipeline.tts._get_http_proxies: возвращает один прокси-словарь.
      - _ffprobe_duration: возвращает 1.0 (не вызываем реальный ffprobe).
    """
    monkeypatch.delenv("FAKE_TTS", raising=False)

    from app.pipeline import tts as tts_mod

    # Счётчик вызовов Communicate
    call_count = 0

    class FakeCommunicate:
        def __init__(self, text, voice, *, boundary=None, proxy=None, **kw):
            nonlocal call_count
            call_count += 1
            self._proxy = proxy

        def stream(self):
            if self._proxy is None:
                # Первый вызов без прокси — провал
                raise RuntimeError("403 Forbidden (simulated)")
            # Второй вызов с прокси — успех
            return _make_fake_audio_chunks()

    # Patch edge_tts.Communicate внутри tts модуля
    fake_edge_tts = MagicMock()
    fake_edge_tts.Communicate = FakeCommunicate

    one_proxy = [{"url": "http://proxy.example.com:3128/", "id": 1}]

    with (
        patch.dict("sys.modules", {"edge_tts": fake_edge_tts}),
        patch.object(tts_mod, "_get_http_proxies", return_value=one_proxy),
        patch.object(tts_mod, "_ffprobe_duration", return_value=1.0),
    ):
        out_path = tmp_path / "voice_01.mp3"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        words = tts_mod._synthesize_one_with_retry(
            "Привет", "ru-RU-DmitryNeural", out_path
        )

    # Должны были быть два вызова: один прямой + один через прокси
    assert call_count == 2, f"Ожидали 2 вызова Communicate, получили {call_count}"
    # Тайминг одного слова должен присутствовать
    assert len(words) == 1, f"Ожидали 1 слово, получили {words}"
    assert words[0][0] == "Привет"


def test_proxy_fallback_error_message(tmp_path, monkeypatch):
    """
    Если все прокси провалились — TTSError содержит подсказку про /proxies.
    """
    monkeypatch.delenv("FAKE_TTS", raising=False)

    from app.pipeline import tts as tts_mod

    class AlwaysFailCommunicate:
        def __init__(self, *a, **kw):
            pass

        def stream(self):
            raise RuntimeError("403 always")

    fake_edge_tts = MagicMock()
    fake_edge_tts.Communicate = AlwaysFailCommunicate

    one_proxy = [{"url": "http://proxy.example.com:3128/", "id": 1}]

    with (
        patch.dict("sys.modules", {"edge_tts": fake_edge_tts}),
        patch.object(tts_mod, "_get_http_proxies", return_value=one_proxy),
    ):
        from app.pipeline.tts import TTSError

        with pytest.raises(TTSError, match="/proxies"):
            tts_mod._synthesize_one_with_retry(
                "Привет", "ru-RU-DmitryNeural", tmp_path / "voice_01.mp3"
            )


# ─── Тесты build_ass ─────────────────────────────────────────────────────────


def _make_words(n: int, start_offset: float = 0.0):
    """Создать N синтетических WordTiming по 0.4 с каждое."""
    from app.pipeline.tts import WordTiming
    dur = 0.4
    return [
        WordTiming(
            word=f"слово{i+1}",
            start=start_offset + i * dur,
            end=start_offset + i * dur + dur,
        )
        for i in range(n)
    ]


def test_ass_play_res(tmp_path):
    """ASS-файл содержит PlayResX=1080 и PlayResY=1920."""
    from app.pipeline.subtitles import build_ass

    words = _make_words(6)
    out = tmp_path / "subs.ass"
    build_ass(words, out)

    content = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content


def test_ass_dialogue_count_default(tmp_path):
    """Число Dialogue-строк == ceil(слов / 3) при words_per_line=3."""
    from app.pipeline.subtitles import build_ass

    n = 7  # 3 + 3 + 1 → 3 строки
    words = _make_words(n)
    out = tmp_path / "subs.ass"
    build_ass(words, out)

    content = out.read_text(encoding="utf-8")
    dialogues = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
    expected = math.ceil(n / 3)
    assert len(dialogues) == expected, (
        f"Ожидалось {expected} Dialogue, найдено {len(dialogues)}"
    )


def test_ass_dialogue_count_custom_wpl(tmp_path):
    """Число Dialogue-строк == ceil(слов / words_per_line) для произвольного WPL."""
    from app.pipeline.subtitles import build_ass

    n = 10
    wpl = 2
    words = _make_words(n)
    out = tmp_path / "subs.ass"
    build_ass(words, out, words_per_line=wpl)

    content = out.read_text(encoding="utf-8")
    dialogues = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
    expected = math.ceil(n / wpl)
    assert len(dialogues) == expected


def test_ass_karaoke_tags_present(tmp_path):
    """В каждой Dialogue-строке должны быть karaoke-теги {\\kNN}."""
    from app.pipeline.subtitles import build_ass

    words = _make_words(6)
    out = tmp_path / "subs.ass"
    build_ass(words, out)

    content = out.read_text(encoding="utf-8")
    dialogues = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
    pattern = re.compile(r"\{\\k\d+\}")
    for d in dialogues:
        assert pattern.search(d), f"Karaoke-тег не найден в строке: {d!r}"


_TIME_RE = re.compile(r"^\d:\d{2}:\d{2}\.\d{2}$")


def test_ass_time_format(tmp_path):
    """Времена в Dialogue должны соответствовать формату H:MM:SS.cc."""
    from app.pipeline.subtitles import build_ass

    words = _make_words(4)
    out = tmp_path / "subs.ass"
    build_ass(words, out)

    content = out.read_text(encoding="utf-8")
    dialogues = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
    for d in dialogues:
        # Формат: Dialogue: 0,H:MM:SS.cc,H:MM:SS.cc,...
        parts = d.split(",")
        start_t = parts[1]
        end_t   = parts[2]
        assert _TIME_RE.match(start_t), f"Неверный формат start: {start_t!r}"
        assert _TIME_RE.match(end_t),   f"Неверный формат end: {end_t!r}"


def test_ass_empty_words(tmp_path):
    """Пустой список слов → валидный .ass без Dialogue-строк."""
    from app.pipeline.subtitles import build_ass

    out = tmp_path / "empty.ass"
    build_ass([], out)

    assert out.exists(), ".ass файл не создан"
    content = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in content
    dialogues = [ln for ln in content.splitlines() if ln.startswith("Dialogue:")]
    assert dialogues == [], f"Ожидали 0 Dialogue, нашли: {dialogues}"


def test_ass_utf8_encoding(tmp_path):
    """Файл должен быть в UTF-8, читаться без ошибок."""
    from app.pipeline.subtitles import build_ass
    from app.pipeline.tts import WordTiming

    words = [
        WordTiming(word="привет", start=0.0, end=0.4),
        WordTiming(word="мир",    start=0.4, end=0.8),
    ]
    out = tmp_path / "cyrillic.ass"
    build_ass(words, out)

    # Должно читаться как UTF-8 без исключений
    content = out.read_text(encoding="utf-8")
    assert "привет" in content or "мир" in content


def test_ass_returns_path(tmp_path):
    """build_ass должна возвращать out_path."""
    from app.pipeline.subtitles import build_ass

    out = tmp_path / "ret.ass"
    result = build_ass([], out)
    assert result == out
