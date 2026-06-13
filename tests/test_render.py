"""
Тесты модуля app/pipeline/render.py (этап 3.3 — рендер вертикальных роликов).

pytest tests/test_render.py -q

Все ffmpeg-тесты под skipif(ffmpeg не найден). Входы готовятся БЕЗ сети:
FAKE_TTS=1 + FAKE_ASSETS=1 → synthesize_scenes + build_ass + fetch_scene_assets.
Музыка — anullsrc mp3 через ffmpeg.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

# ─── Маркер доступности ffmpeg ────────────────────────────────────────────────

ffmpeg_available = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe не найдены в PATH",
)


# ─── ffprobe-хелперы ──────────────────────────────────────────────────────────


def _probe_streams(path: Path) -> dict:
    """Вернуть {'video': {...} | None, 'audio': {...} | None} через ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,width,height",
        "-of", "json",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    import json
    data = json.loads(out.stdout)
    result = {"video": None, "audio": None}
    for s in data.get("streams", []):
        if s["codec_type"] == "video":
            result["video"] = s
        elif s["codec_type"] == "audio":
            result["audio"] = s
    return result


def _probe_duration(path: Path) -> float:
    """Длительность контейнера через ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(path),
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return float(out.stdout.strip())


def _make_fake_music(dest: Path, duration: float = 3.0) -> Path:
    """Создать тихий mp3 нужной длины через ffmpeg anullsrc."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(duration),
        "-q:a", "9",
        str(dest),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr.decode(errors="replace")
    return dest


# ─── Подготовка входов (без сети) ─────────────────────────────────────────────


def _prepare_inputs(tmp_path, monkeypatch, content_id: int, template: str, n_scenes: int = 2):
    """
    Подготовить входы для render_video через FAKE_TTS + FAKE_ASSETS.

    Возвращает (scene_audios, asset_paths, subs_path).
    template: 'video_footage' (mp4) | 'video_slideshow' (jpg).
    """
    monkeypatch.setenv("FAKE_TTS", "1")
    monkeypatch.setenv("FAKE_ASSETS", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.tts import synthesize_scenes
    from app.pipeline.subtitles import build_ass
    from app.pipeline.assets import fetch_scene_assets

    scene_texts = [f"Короткая сцена номер {i + 1} тут" for i in range(n_scenes)]
    media_dir = tmp_path / "media" / str(content_id)
    scene_audios = synthesize_scenes(scene_texts, media_dir / "audio")

    scenes = [{"text": t, "keywords": ["nature", "sky"]} for t in scene_texts]
    asset_paths = fetch_scene_assets(content_id, scenes, template)

    all_words = [w for sa in scene_audios for w in sa.words]
    subs_path = build_ass(all_words, media_dir / "subs.ass")

    return scene_audios, asset_paths, subs_path


def _assert_valid_video(video_path: Path, preview_path: Path, total_dur: float):
    """Общие проверки результата рендера."""
    assert video_path.exists(), "Видео не создано"
    assert video_path.stat().st_size > 0, "Видео пустое"
    assert preview_path.exists(), "Превью не создано"
    assert preview_path.stat().st_size > 0, "Превью пустое"

    streams = _probe_streams(video_path)
    assert streams["video"] is not None, "Нет видеопотока"
    assert streams["audio"] is not None, "Нет аудиопотока"
    assert streams["video"]["width"] == 1080
    assert streams["video"]["height"] == 1920

    dur = _probe_duration(video_path)
    assert abs(dur - total_dur) <= 0.7, f"Длительность {dur:.2f} != ожидаемой {total_dur:.2f}"

    pv = _probe_streams(preview_path)
    assert pv["video"] is not None
    assert pv["video"]["width"] == 540
    assert pv["video"]["height"] == 960


# ─── Юнит-тесты build_ass: цвета и стиль ─────────────────────────────────────


def test_build_ass_default_colors(tmp_path):
    """
    Дефолтные цвета: PrimaryColour=белый (#ffffff), SecondaryColour=белый.
    Подсветка #ffe600 присутствует как inline-тег {\\1c&H00E6FF&} в Dialogue.
    OutlineColour=чёрный (#000000 → &H00000000).
    """
    from app.pipeline.tts import WordTiming
    from app.pipeline.subtitles import build_ass

    words = [
        WordTiming("Привет", 0.0, 0.4),
        WordTiming("мир",    0.4, 0.8),
    ]
    out = build_ass(words, tmp_path / "subs.ass")
    content = out.read_text(encoding="utf-8")

    # PrimaryColour = font_color (белый #ffffff → &H00FFFFFF) — в Style
    assert "&H00FFFFFF" in content, "PrimaryColour/SecondaryColour (белый) не найден в Style"
    # OutlineColour = outline (чёрный #000000 → &H00000000) — в Style
    assert "&H00000000" in content, "OutlineColour (обрамление) не совпадает"
    # Подсветка #ffe600 inline в Dialogue: BGR = 00E6FF → &H00E6FF&
    assert "{\\1c&H00E6FF&}" in content, "Inline-тег подсветки жёлтого цвета не найден"


def test_build_ass_custom_colors(tmp_path):
    """Пользовательские цвета корректно попадают в .ass."""
    from app.pipeline.tts import WordTiming
    from app.pipeline.subtitles import build_ass

    words = [WordTiming("Тест", 0.0, 0.4)]
    out = build_ass(
        words,
        tmp_path / "custom.ass",
        font_color="#ff0000",        # красный → Primary/Secondary &H000000FF
        outline_color="#00ff00",     # зелёный → OutlineColour &H0000FF00
        outline_width=3,
        highlight_color="#0000ff",   # синий inline → &HFF0000&
    )
    content = out.read_text(encoding="utf-8")

    # PrimaryColour = font_color red #ff0000 → &H000000FF — в Style
    assert "&H000000FF" in content, "PrimaryColour (красный) не найден в Style"
    # OutlineColour = outline green #00ff00 → &H0000FF00
    assert "&H0000FF00" in content, "OutlineColour (зелёный) не найден"
    # Подсветка #0000ff inline: BGR = FF0000 → &HFF0000&
    assert "{\\1c&HFF0000&}" in content, "Inline-тег подсветки синего цвета не найден"
    # Outline width
    assert ",3," in content, "Толщина обрамления 3 не найдена"


def test_build_ass_format_line_has_color_fields(tmp_path):
    """Format-строка стилей содержит поля цветов."""
    from app.pipeline.tts import WordTiming
    from app.pipeline.subtitles import build_ass

    out = build_ass([], tmp_path / "empty.ass")
    content = out.read_text(encoding="utf-8")

    assert "PrimaryColour" in content
    assert "SecondaryColour" in content
    assert "OutlineColour" in content
    assert "BackColour" in content


def test_build_ass_outline_width_applied(tmp_path):
    """Параметр outline_width корректно передаётся в строку Style."""
    from app.pipeline.tts import WordTiming
    from app.pipeline.subtitles import build_ass

    out = build_ass(
        [WordTiming("слово", 0.0, 0.4)],
        tmp_path / "width.ass",
        outline_width=7,
    )
    content = out.read_text(encoding="utf-8")
    # Толщина 7 должна быть в строке Style
    assert ",7," in content, "Толщина обрамления 7 не найдена в Style"


# ─── Юнит-тест экранирования пути (без ffmpeg) ────────────────────────────────


def test_escape_subtitles_path_windows():
    """Windows-путь: диск C: экранируется, backslash → слэш."""
    from app.pipeline.render import escape_subtitles_path

    result = escape_subtitles_path(Path(r"C:\data\media\5\subs.ass"))
    # двоеточие диска экранировано, разделители — прямые слэши
    assert result == r"C\:/data/media/5/subs.ass"
    assert "\\:" in result, "Двоеточие не экранировано"
    assert "\\d" not in result, "Backslash перед путём не убран"


def test_escape_subtitles_path_no_colon():
    """POSIX-подобный путь без двоеточия не ломается."""
    from app.pipeline.render import escape_subtitles_path

    result = escape_subtitles_path(Path("/data/media/subs.ass"))
    assert result == "/data/media/subs.ass"


def test_escape_subtitles_path_relative():
    """Относительный путь с backslash нормализуется в прямые слэши."""
    from app.pipeline.render import escape_subtitles_path

    result = escape_subtitles_path(Path("media") / "5" / "subs.ass")
    assert "\\" not in result.replace("\\:", "")  # нет невыеэкранированных backslash
    assert result.endswith("subs.ass")


# ─── Валидация входов (без ffmpeg) ────────────────────────────────────────────


def test_render_mismatched_lengths(tmp_path, monkeypatch):
    """len(asset_paths) != len(scene_audios) → RenderError."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.pipeline.render import render_video, RenderError
    from app.pipeline.tts import SceneAudio

    sa = [SceneAudio(index=0, path=tmp_path / "a.mp3", duration=1.0, words=[])]
    subs = tmp_path / "s.ass"
    subs.write_text("[Script Info]\n", encoding="utf-8")

    with pytest.raises(RenderError):
        render_video(1, sa, [], subs, None)


# ─── Рендер: футажи (mp4) ─────────────────────────────────────────────────────


@ffmpeg_available
def test_render_footage_no_music(tmp_path, monkeypatch):
    """Шаблон футажей (mp4), без музыки → валидное видео + превью."""
    from app.pipeline.render import render_video

    scene_audios, assets, subs = _prepare_inputs(
        tmp_path, monkeypatch, content_id=100, template="video_footage", n_scenes=3
    )
    total = sum(sa.duration for sa in scene_audios)

    video, preview = render_video(100, scene_audios, assets, subs, None)
    _assert_valid_video(video, preview, total)

    # render_tmp подчищен
    tmp_render = tmp_path / "media" / "100" / "render_tmp"
    assert not tmp_render.exists(), "render_tmp не подчищен после успеха"


@ffmpeg_available
def test_render_footage_with_music(tmp_path, monkeypatch):
    """Шаблон футажей (mp4), с музыкой (ducking) → валидное видео."""
    from app.pipeline.render import render_video

    scene_audios, assets, subs = _prepare_inputs(
        tmp_path, monkeypatch, content_id=101, template="video_footage", n_scenes=2
    )
    total = sum(sa.duration for sa in scene_audios)
    music = _make_fake_music(tmp_path / "bg.mp3", duration=3.0)

    video, preview = render_video(101, scene_audios, assets, subs, music)
    _assert_valid_video(video, preview, total)


# ─── Рендер: слайдшоу (jpg, Ken Burns) ────────────────────────────────────────


@ffmpeg_available
def test_render_slideshow_no_music(tmp_path, monkeypatch):
    """Шаблон слайдшоу (jpg, Ken Burns), без музыки → валидное видео."""
    from app.pipeline.render import render_video

    scene_audios, assets, subs = _prepare_inputs(
        tmp_path, monkeypatch, content_id=200, template="video_slideshow", n_scenes=2
    )
    assert all(p.suffix == ".jpg" for p in assets)
    total = sum(sa.duration for sa in scene_audios)

    video, preview = render_video(200, scene_audios, assets, subs, None)
    _assert_valid_video(video, preview, total)


@ffmpeg_available
def test_render_slideshow_with_music(tmp_path, monkeypatch):
    """Шаблон слайдшоу (jpg), с музыкой → валидное видео."""
    from app.pipeline.render import render_video

    scene_audios, assets, subs = _prepare_inputs(
        tmp_path, monkeypatch, content_id=201, template="video_slideshow", n_scenes=2
    )
    total = sum(sa.duration for sa in scene_audios)
    music = _make_fake_music(tmp_path / "bg2.mp3", duration=2.0)

    video, preview = render_video(201, scene_audios, assets, subs, music)
    _assert_valid_video(video, preview, total)


# ─── Рендер: смешанные ассеты (mp4 + jpg) ─────────────────────────────────────


# ─── Тест music_volume в filter_complex (без ffmpeg) ─────────────────────────


def test_final_mux_custom_music_volume(tmp_path, monkeypatch):
    """_final_mux с music_volume=0.3 формирует filter_complex с volume=0.3."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.render import _final_mux, _MUSIC_BASE_VOLUME

    # Подготовим фиктивные пути (файлы создаём, чтобы ffmpeg не падал на проверке)
    silent = tmp_path / "silent.mp4"
    voice = tmp_path / "voice.m4a"
    music = tmp_path / "music.mp3"
    subs = tmp_path / "subs.ass"
    out = tmp_path / "out.mp4"
    for f in (silent, voice, music, subs):
        f.write_bytes(b"FAKE")

    captured_cmds = []

    def fake_run_ffmpeg(cmd, label=""):
        captured_cmds.append(cmd)

    import app.pipeline.render as render_module
    monkeypatch.setattr(render_module, "_run_ffmpeg", fake_run_ffmpeg)

    _final_mux(silent, voice, music, subs, 5.0, out, music_volume=0.3)

    assert captured_cmds, "ffmpeg не был вызван"
    cmd_str = " ".join(str(a) for a in captured_cmds[0])
    assert "volume=0.3" in cmd_str, f"volume=0.3 не найден в cmd: {cmd_str}"
    # Убедимся, что base_volume НЕ попал в команду (он мог бы быть 0.25)
    assert "volume=0.25" not in cmd_str, "Старый _MUSIC_BASE_VOLUME=0.25 не должен быть в команде"


def test_final_mux_default_music_volume_is_base(tmp_path, monkeypatch):
    """_final_mux без music_volume использует _MUSIC_BASE_VOLUME=0.25."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.render import _final_mux, _MUSIC_BASE_VOLUME

    silent = tmp_path / "silent2.mp4"
    voice = tmp_path / "voice2.m4a"
    music = tmp_path / "music2.mp3"
    subs = tmp_path / "subs2.ass"
    out = tmp_path / "out2.mp4"
    for f in (silent, voice, music, subs):
        f.write_bytes(b"FAKE")

    captured_cmds = []

    def fake_run_ffmpeg(cmd, label=""):
        captured_cmds.append(cmd)

    import app.pipeline.render as render_module
    monkeypatch.setattr(render_module, "_run_ffmpeg", fake_run_ffmpeg)

    _final_mux(silent, voice, music, subs, 5.0, out)

    assert captured_cmds, "ffmpeg не был вызван"
    cmd_str = " ".join(str(a) for a in captured_cmds[0])
    expected = f"volume={_MUSIC_BASE_VOLUME}"
    assert expected in cmd_str, f"{expected} не найден в cmd: {cmd_str}"


def test_render_video_passes_music_volume(tmp_path, monkeypatch):
    """render_video с music_volume=0.3 передаёт его в _final_mux."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FAKE_TTS", "1")
    monkeypatch.setenv("FAKE_ASSETS", "1")

    captured_volumes = []

    import app.pipeline.render as render_module

    original_final_mux = render_module._final_mux

    def spy_final_mux(*args, **kwargs):
        # music_volume — 7-й позиционный аргумент (индекс 6)
        if len(args) > 6:
            captured_volumes.append(args[6])
        elif "music_volume" in kwargs:
            captured_volumes.append(kwargs["music_volume"])
        # Заменяем вызов, чтобы не запускать ffmpeg
        return None

    monkeypatch.setattr(render_module, "_run_ffmpeg", lambda cmd, label="": None)
    monkeypatch.setattr(render_module, "_final_mux", spy_final_mux)

    from app.pipeline.tts import synthesize_scenes
    from app.pipeline.subtitles import build_ass
    from app.pipeline.assets import fetch_scene_assets
    from app.pipeline.render import render_video, RenderError

    cid = 999
    scene_texts = ["Тестовая сцена"]
    media_dir = tmp_path / "media" / str(cid)
    scene_audios = synthesize_scenes(scene_texts, media_dir / "audio")
    scenes = [{"text": t, "keywords": ["test"]} for t in scene_texts]
    asset_paths = fetch_scene_assets(cid, scenes, "video_slideshow")
    all_words = [w for sa in scene_audios for w in sa.words]
    subs_path = build_ass(all_words, media_dir / "subs.ass")

    music = tmp_path / "bg.mp3"
    music.write_bytes(b"FAKE_MUSIC")

    # render_video вызовет _concat_clips и _concat_voice тоже через _run_ffmpeg,
    # они замоканы и вернут None — итоговые файлы не создадутся,
    # поэтому ловим любое исключение после spy_final_mux
    try:
        render_video(cid, scene_audios, asset_paths, subs_path, music, music_volume=0.3)
    except Exception:
        pass  # после мока ffmpeg файлы не создаются — ожидаемо

    assert captured_volumes, "spy_final_mux не был вызван"
    assert abs(captured_volumes[0] - 0.3) < 1e-9, f"Ожидался volume=0.3, получен {captured_volumes[0]}"


@ffmpeg_available
def test_render_mixed_assets(tmp_path, monkeypatch):
    """Один ролик: сцена 1 = mp4-футаж, сцена 2 = jpg (Ken Burns)."""
    monkeypatch.setenv("FAKE_TTS", "1")
    monkeypatch.setenv("FAKE_ASSETS", "1")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from app.pipeline.tts import synthesize_scenes
    from app.pipeline.subtitles import build_ass
    from app.pipeline.assets import fetch_scene_assets
    from app.pipeline.render import render_video

    cid = 300
    scene_texts = ["Первая сцена видео", "Вторая сцена картинка"]
    media_dir = tmp_path / "media" / str(cid)
    scene_audios = synthesize_scenes(scene_texts, media_dir / "audio")

    scenes = [{"text": t, "keywords": ["city"]} for t in scene_texts]
    # сцена 1 — футаж (mp4), сцена 2 — картинка (jpg)
    footage = fetch_scene_assets(cid, [scenes[0]], "video_footage")
    slideshow = fetch_scene_assets(cid + 1, [scenes[1]], "video_slideshow")
    assets = [footage[0], slideshow[0]]
    assert assets[0].suffix == ".mp4"
    assert assets[1].suffix == ".jpg"

    all_words = [w for sa in scene_audios for w in sa.words]
    subs = build_ass(all_words, media_dir / "subs.ass")
    total = sum(sa.duration for sa in scene_audios)

    video, preview = render_video(cid, scene_audios, assets, subs, None)
    _assert_valid_video(video, preview, total)
