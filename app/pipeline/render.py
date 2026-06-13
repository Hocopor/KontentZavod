"""
Рендер вертикальных роликов 1080×1920 через ffmpeg filter graph.

Публичный API:
    render_video(content_id, scene_audios, asset_paths, subs_path, music_path)
        -> (video_path, preview_path)

Архитектура (под сервер 4GB RAM / 2 vCPU — НЕ строим один гигантский
filter_complex на все сцены, рендерим поэтапно):

    Проход 1 (на каждую сцену): из одного ассета сделать немой клип
        scene_NN.mp4 (1080×1920, fps30, yuv420p) длиной ровно в сцену.
          • .mp4-футаж → scale+crop по центру, обрезка/зацикливание до длительности;
          • .jpg-картинка → Ken Burns (zoompan, зум ~1.0→1.12).
    Проход 2: concat demuxer склеивает немые клипы БЕЗ перекодирования
        (-c copy) в silent_full.mp4.
    Проход 3: голосовые mp3 склеиваются в один voice.m4a (concat-фильтр).
    Проход 4 (финал): video(silent_full) + audio(voice [+ music с ducking
        через sidechaincompress]) + вжигание субтитров (subtitles=, libass).
    Проход 5: превью — один кадр ~0.5с, scale 540×960 → {content_id}.jpg.

Все вызовы ffmpeg через subprocess.run(capture_output=True); при ненулевом
коде — RenderError с хвостом stderr. Временные файлы — в
{DATA_DIR}/media/{content_id}/render_tmp/, подчищаются после успеха.
"""
import logging
import shutil
import subprocess
from pathlib import Path

from app.config import settings
from app.pipeline.tts import SceneAudio

logger = logging.getLogger(__name__)

# ─── Константы вывода ────────────────────────────────────────────────────────

_W = 1080
_H = 1920
_FPS = 30
_PREVIEW_W = 540
_PREVIEW_H = 960
_PREVIEW_AT = 0.5          # секунда кадра для превью

# Музыка базово приглушена и приседает под голосом (ducking)
_MUSIC_BASE_VOLUME = 0.25

_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# Хвост stderr в сообщении об ошибке
_STDERR_TAIL = 500
_FFMPEG_TIMEOUT = 600      # сек на один вызов


# ─── Исключения ──────────────────────────────────────────────────────────────


class RenderError(Exception):
    """Ошибка рендера видео (ffmpeg вернул ненулевой код, неверные входы)."""


# ─── Вспомогательные функции ──────────────────────────────────────────────────


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    """
    Запустить ffmpeg-команду. При ненулевом коде поднять RenderError
    с хвостом stderr (последние ~500 символов).
    """
    logger.debug("ffmpeg [%s]: %s", what, " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"ffmpeg завис на этапе «{what}»") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        tail = stderr[-_STDERR_TAIL:]
        raise RenderError(
            f"ffmpeg ошибка на этапе «{what}» (код {result.returncode}): …{tail}"
        )


def escape_subtitles_path(path: Path) -> str:
    """
    Экранировать путь к .ass/.srt для использования внутри фильтра subtitles=.

    Грабли Windows: внутри значения фильтра двоеточие диска и обратные слэши
    интерпретируются ffmpeg-парсером фильтров. Нужно:
      • заменить '\\' на '/';
      • экранировать ':' как '\\:' (двоеточие диска C: → C\\:).

    Пример: C:\\data\\sub.ass  →  C\\:/data/sub.ass

    Возвращает строку, готовую к подстановке как subtitles='<...>'.
    """
    # Нормализуем разделители в прямые слэши (libass/ffmpeg их понимает на всех ОС)
    s = str(path).replace("\\", "/")
    # Экранируем двоеточие (диск C: и любые прочие)
    s = s.replace(":", "\\:")
    return s


def _is_video(path: Path) -> bool:
    """Определить тип ассета по расширению: видеофутаж или картинка."""
    ext = path.suffix.lower()
    if ext in _VIDEO_EXTS:
        return True
    if ext in _IMAGE_EXTS:
        return False
    raise RenderError(f"Неизвестное расширение ассета: {path.name}")


# ─── Проход 1: один сценный клип (немой) ──────────────────────────────────────


def _render_scene_clip(asset: Path, duration: float, out_path: Path) -> None:
    """
    Сделать из одного ассета немой клип 1080×1920, fps30, длиной `duration`.

    Видеофутаж: stream_loop -1 (зацикливание для коротких футажей) +
        scale до покрытия 1080×1920 + crop по центру + обрезка по -t.
    Картинка: loop одного кадра + zoompan (Ken Burns, зум 1.0→~1.12).
    """
    if _is_video(asset):
        # масштаб «по большей стороне» → центр-кроп → fps → длительность
        vf = (
            f"scale={_W}:{_H}:force_original_aspect_ratio=increase,"
            f"crop={_W}:{_H},"
            f"fps={_FPS},"
            f"setsar=1"
        )
        cmd = [
            settings.FFMPEG_BIN, "-y",
            "-stream_loop", "-1",          # зациклить короткий футаж
            "-i", str(asset),
            "-t", f"{duration:.3f}",
            "-an",                          # без звука
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "26",
            "-pix_fmt", "yuv420p",
            str(out_path),
        ]
        _run_ffmpeg(cmd, f"сценный клип (видео) {asset.name}")
        return

    # ── Картинка: Ken Burns через zoompan ───────────────────────────────────
    # Кол-во кадров zoompan = duration * fps. Зум линейно 1.0 → 1.12.
    frames = max(1, int(round(duration * _FPS)))
    zoom_end = 1.12
    # zoom инкремент за кадр; ограничиваем сверху zoom_end
    # 'on' — текущий номер выходного кадра zoompan
    zoom_expr = f"min(zoom+{(zoom_end - 1.0) / frames:.6f},{zoom_end})"
    # Предварительно апскейлим источник (zoompan работает чётче на крупном входе),
    # затем zoompan на нужный размер, центрируем точку зума.
    vf = (
        f"scale={_W * 2}:{_H * 2}:force_original_aspect_ratio=increase,"
        f"crop={_W * 2}:{_H * 2},"
        f"zoompan=z='{zoom_expr}':"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={_W}x{_H}:fps={_FPS},"
        f"setsar=1"
    )
    cmd = [
        settings.FFMPEG_BIN, "-y",
        "-loop", "1",
        "-i", str(asset),
        "-t", f"{duration:.3f}",
        "-an",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "26",
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    _run_ffmpeg(cmd, f"сценный клип (картинка/KenBurns) {asset.name}")


# ─── Проход 2: склейка немых клипов (concat demuxer, без перекодирования) ──────


def _concat_clips(clips: list[Path], tmp_dir: Path, out_path: Path) -> None:
    """
    Склеить немые клипы concat-демуксером БЕЗ перекодирования (-c copy).
    Все клипы рендерились одним кодеком/параметрами → copy безопасен.
    """
    list_file = tmp_dir / "concat_video.txt"
    # concat demuxer: одинарные кавычки вокруг пути, прямые слэши
    lines = [f"file '{str(c).replace(chr(92), '/')}'" for c in clips]
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        settings.FFMPEG_BIN, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        str(out_path),
    ]
    _run_ffmpeg(cmd, "склейка немых клипов (concat copy)")


# ─── Проход 3: склейка голосовых дорожек ──────────────────────────────────────


def _concat_voice(scene_audios: list[SceneAudio], tmp_dir: Path, out_path: Path) -> None:
    """
    Склеить голосовые mp3 сцен в один аудиофайл (AAC m4a) через adelay+amix.

    Вместо concat-фильтра каждая сцена якорится в свой точный глобальный offset
    через adelay=<ms>:all=1, затем все дорожки смешиваются amix(normalize=0).
    Это устраняет накопление mp3 priming-тишины (без LAME/Xing-заголовка concat
    копил паузу с каждой сценой → голос прогрессивно отставал от субтитров).
    Перекрытий нет (сцены идут подряд) → суммирование без клиппинга, полная громкость.
    Требует ffmpeg >= 4.4 (adelay поддержка all=1).
    """
    cmd = [settings.FFMPEG_BIN, "-y"]
    for sa in scene_audios:
        cmd += ["-i", str(sa.path)]

    n = len(scene_audios)
    filters: list[str] = []
    labels: list[str] = []
    offset: float = 0.0

    for i, sa in enumerate(scene_audios):
        delay_ms = int(round(offset * 1000))
        filters.append(f"[{i}:a]adelay={delay_ms}:all=1[d{i}]")
        labels.append(f"[d{i}]")
        offset += sa.duration

    mix = "".join(labels) + f"amix=inputs={n}:normalize=0[out]"
    filter_complex = ";".join(filters + [mix])

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-c:a", "aac",
        "-b:a", "128k",
        str(out_path),
    ]
    _run_ffmpeg(cmd, "склейка голосовых дорожек (adelay-якорь)")


# ─── Проход 4: финальная сборка (видео + аудио + субтитры) ────────────────────


def _final_mux(
    silent_video: Path,
    voice_audio: Path,
    music_path: Path | None,
    subs_path: Path,
    total_duration: float,
    out_path: Path,
) -> None:
    """
    Финальный проход: немое видео + голос (+ музыка с ducking) + вжигание ASS.

    Музыка: зацикливается на всю длительность, базовая громкость 0.25,
    приседает под голосом через sidechaincompress (голос — sidechain-вход).
    """
    subs_arg = escape_subtitles_path(subs_path)

    cmd = [settings.FFMPEG_BIN, "-y"]
    cmd += ["-i", str(silent_video)]      # 0: видео
    cmd += ["-i", str(voice_audio)]       # 1: голос

    if music_path is not None:
        # 2: музыка, зацикленная на входе
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]

    # ── Видеоцепочка: вжигаем субтитры ──
    video_chain = f"[0:v]subtitles='{subs_arg}'[v]"

    if music_path is not None:
        # Голос делим на два: один в микс, второй — sidechain для компрессора.
        # Музыка: громкость → ducking под голосом → обрезка по длительности.
        audio_chain = (
            f"[1:a]asplit=2[voice_mix][voice_sc];"
            f"[2:a]volume={_MUSIC_BASE_VOLUME}[music_low];"
            f"[music_low][voice_sc]sidechaincompress="
            f"threshold=0.05:ratio=8:attack=20:release=300[music_ducked];"
            f"[voice_mix][music_ducked]amix=inputs=2:duration=first:"
            f"dropout_transition=0[a]"
        )
        filter_complex = f"{video_chain};{audio_chain}"
        a_map = "[a]"
    else:
        filter_complex = video_chain
        a_map = "1:a"

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", a_map,
        "-t", f"{total_duration:.3f}",
        "-r", str(_FPS),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(out_path),
    ]
    _run_ffmpeg(cmd, "финальная сборка (видео+аудио+субтитры)")


# ─── Проход 5: превью ─────────────────────────────────────────────────────────


def _render_preview(video_path: Path, out_path: Path) -> None:
    """Извлечь кадр ~0.5с и отмасштабировать до 540×960 → jpg."""
    cmd = [
        settings.FFMPEG_BIN, "-y",
        "-ss", f"{_PREVIEW_AT:.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", f"scale={_PREVIEW_W}:{_PREVIEW_H}",
        "-q:v", "3",
        str(out_path),
    ]
    _run_ffmpeg(cmd, "превью")


# ─── Основная публичная функция ───────────────────────────────────────────────


def render_video(
    content_id: int,
    scene_audios: list[SceneAudio],
    asset_paths: list[Path],
    subs_path: Path,
    music_path: Path | None,
) -> tuple[Path, Path]:
    """
    Срендерить вертикальный ролик 1080×1920 из сценных ассетов, озвучки и субтитров.

    Args:
        content_id:   id контента (используется в путях вывода).
        scene_audios: список SceneAudio (по одному на сцену), задаёт длительности.
        asset_paths:  пути к ассетам сцен (.mp4 — футаж, .jpg — картинка),
                      len == len(scene_audios).
        subs_path:    путь к .ass-субтитрам для вжигания.
        music_path:   путь к mp3 фоновой музыки или None.

    Returns:
        (video_path, preview_path):
            video_path   — {DATA_DIR}/videos/{content_id}.mp4
            preview_path — {DATA_DIR}/videos/{content_id}.jpg

    Raises:
        RenderError: несовпадение длин входов, отсутствие файлов, ошибка ffmpeg.
    """
    if not scene_audios:
        raise RenderError("Пустой список scene_audios — нечего рендерить")
    if len(asset_paths) != len(scene_audios):
        raise RenderError(
            f"len(asset_paths)={len(asset_paths)} != "
            f"len(scene_audios)={len(scene_audios)}"
        )
    if not subs_path.exists():
        raise RenderError(f"Файл субтитров не найден: {subs_path}")
    for p in asset_paths:
        if not p.exists():
            raise RenderError(f"Ассет не найден: {p}")

    total_duration = sum(sa.duration for sa in scene_audios)
    if total_duration <= 0:
        raise RenderError(f"Суммарная длительность <= 0: {total_duration}")

    # ── Директории ──
    videos_dir = settings.data_dir_absolute / "videos"
    videos_dir.mkdir(parents=True, exist_ok=True)
    video_path = videos_dir / f"{content_id}.mp4"
    preview_path = videos_dir / f"{content_id}.jpg"

    tmp_dir = settings.data_dir_absolute / "media" / str(content_id) / "render_tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Проход 1: сценные немые клипы ──
        scene_clips: list[Path] = []
        for i, (sa, asset) in enumerate(zip(scene_audios, asset_paths)):
            clip = tmp_dir / f"clip_{i + 1:02d}.mp4"
            _render_scene_clip(asset, sa.duration, clip)
            scene_clips.append(clip)
            logger.debug("Сцена %d: клип готов (%.2f с)", i + 1, sa.duration)

        # ── Проход 2: склейка видео ──
        silent_full = tmp_dir / "silent_full.mp4"
        _concat_clips(scene_clips, tmp_dir, silent_full)

        # ── Проход 3: склейка голоса ──
        voice_audio = tmp_dir / "voice.m4a"
        _concat_voice(scene_audios, tmp_dir, voice_audio)

        # ── Проход 4: финальная сборка ──
        _final_mux(
            silent_full,
            voice_audio,
            music_path,
            subs_path,
            total_duration,
            video_path,
        )

        # ── Проход 5: превью ──
        _render_preview(video_path, preview_path)

    except Exception:
        # При ошибке временные файлы оставляем для диагностики — пробрасываем.
        raise

    # ── Подчистка временных файлов после успеха ──
    shutil.rmtree(tmp_dir, ignore_errors=True)

    logger.info(
        "Рендер content_id=%d готов: %s (%.2f с), превью %s",
        content_id, video_path.name, total_duration, preview_path.name,
    )
    return video_path, preview_path
