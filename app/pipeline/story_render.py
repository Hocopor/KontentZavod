"""Рендер слайдов сторис (этап 8.5): наложение текста на картинку 1080×1920
через ASS/libass + ffmpeg. Эмодзи из текста наложения вырезаются (libass без
emoji-шрифта даёт «тофу»); в подписи поста эмодзи остаются."""
import logging
import re
import subprocess
from pathlib import Path

from app.config import settings
from app.pipeline.assets import fetch_image
from app.pipeline.render import escape_subtitles_path

logger = logging.getLogger(__name__)

# Unicode-диапазоны эмодзи и пиктограмм для вырезания из текста наложения
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F0FF"
    "\U00002190-\U000021FF\U0000FE00-\U0000FE0F\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF]+",
    flags=re.UNICODE,
)

_ALIGN = {"top": 8, "center": 5, "bottom": 2}      # ASS \an numpad-нумерация
_MARGINV = {"top": 200, "center": 0, "bottom": 200}


def _strip_emoji(text: str) -> str:
    """Убрать эмодзи/пиктограммы из текста наложения. Кириллицу/латиницу сохраняет."""
    return _EMOJI_RE.sub("", text or "").strip()


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    """Запустить ffmpeg, бросить RuntimeError при ненулевом коде (как в render.py)."""
    logger.info("ffmpeg (%s): %s", what, " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg ({what}) код {proc.returncode}: {proc.stderr[-800:]}")


def _build_slide_ass(text: str, position: str, font: str, font_size: int) -> str:
    """Собрать ASS для одного слайда: белый жирный текст на плашке (BorderStyle=3)."""
    align = _ALIGN.get(position, 5)
    marginv = _MARGINV.get(position, 0)
    ass_text = (text or "").replace("\n", "\\N")
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "WrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: S,{font},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,1,0,0,0,"
        f"100,100,0,0,3,8,0,{align},90,90,{marginv},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:05.00,S,,0,0,0,,{ass_text}\n"
    )


def _render_solid_bg(bg: Path) -> None:
    """Фон-заглушка (нейтральный цвет 1080×1920) — если fetch_image ничего не нашёл."""
    cmd = [settings.FFMPEG_BIN, "-y", "-f", "lavfi", "-i", "color=c=0x2a4d69:s=1080x1920",
           "-frames:v", "1", str(bg)]
    _run_ffmpeg(cmd, "story bg fallback")


def render_story_slides(slides: list[dict], out_dir, *, font: str | None = None,
                        font_size: int | None = None) -> list[Path]:
    """Отрендерить слайды сторис → список путей slide_1.jpg..slide_N.jpg (по порядку).

    slides: [{"text","image_keywords":[...],"position":"center|top|bottom"}].
    Эмодзи из text вырезаются. Фон — fetch_image по keywords, иначе заглушка.
    """
    font = font or settings.STORY_FONT
    font_size = font_size or settings.STORY_FONT_SIZE
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for i, slide in enumerate(slides, 1):
        bg = out_dir / f"bg_{i}.jpg"
        try:
            ok = fetch_image(slide.get("image_keywords") or [], bg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("story slide %d: fetch_image упал (%s) — фон-заглушка", i, exc)
            ok = False
        if not ok:
            _render_solid_bg(bg)
        text = _strip_emoji(slide.get("text") or "")
        ass_path = out_dir / f"slide_{i}.ass"
        ass_path.write_text(
            _build_slide_ass(text, slide.get("position") or "center", font, font_size),
            encoding="utf-8",
        )
        out_path = out_dir / f"slide_{i}.jpg"
        subs = escape_subtitles_path(ass_path)
        cmd = [settings.FFMPEG_BIN, "-y", "-loop", "1", "-i", str(bg),
               "-vf", f"scale=1080:1920:force_original_aspect_ratio=increase,"
                      f"crop=1080:1920,subtitles='{subs}'",
               "-frames:v", "1", str(out_path)]
        _run_ffmpeg(cmd, f"story slide {i}")
        paths.append(out_path)
    return paths
