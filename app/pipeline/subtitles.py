"""
Генерация ASS-субтитров для вертикальных видео 1080×1920.

Публичный API:
    build_ass(words, out_path, words_per_line=3) -> Path

Особенности:
  - PlayResX=1080, PlayResY=1920 (вертикальный формат).
  - Стиль «Sub»: Arial 110 Bold, белый с жёлтым karaoke-подсветкой.
  - Блок текста выровнен по нижней трети экрана (MarginV=550),
    не перекрывает UI Shorts/Reels.
  - Каждая группа из words_per_line слов — один Dialogue.
  - Каждое слово предваряется тегом {\\kNN}, где NN — длительность
    в сантисекундах; паузы между словами добавляются к {\\k} предыдущего.
"""
import math
from pathlib import Path

from app.pipeline.tts import WordTiming

# ─── Константы стиля ─────────────────────────────────────────────────────────

_PLAY_RES_X = 1080
_PLAY_RES_Y = 1920

# ASS-цвет: формат AABBGGRR (alpha, blue, green, red)
_WHITE  = "&H00FFFFFF"   # PrimaryColour — белый
_YELLOW = "&H0000FFFF"   # SecondaryColour — жёлтый (karaoke-подсветка)

_STYLE_LINE = (
    "Style: Sub,"           # Имя стиля
    "Arial,"                # Fontname
    "110,"                  # Fontsize
    "1,"                    # Bold
    "0,"                    # Italic
    "0,"                    # Underline
    "0,"                    # StrikeOut
    "100,"                  # ScaleX
    "100,"                  # ScaleY
    "0,"                    # Spacing
    "0,"                    # Angle
    "1,"                    # BorderStyle (outline+shadow)
    "5,"                    # Outline
    "2,"                    # Shadow
    "2,"                    # Alignment (нижний центр)
    "10,"                   # MarginL
    "10,"                   # MarginR
    "550,"                  # MarginV — выше нижней трети
    "0"                     # Encoding
)

# ─── Вспомогательные функции ─────────────────────────────────────────────────


def _to_ass_time(seconds: float) -> str:
    """
    Конвертировать секунды в формат ASS: H:MM:SS.cc
    (cc — сотые доли секунды).
    """
    seconds = max(0.0, seconds)
    total_cs = int(round(seconds * 100))
    cs  = total_cs % 100
    s   = (total_cs // 100) % 60
    m   = (total_cs // 6000) % 60
    h   = total_cs // 360000
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _karaoke_line(words: list[WordTiming]) -> str:
    """
    Сформировать строку текста с karaoke-тегами {\\kNN} для группы слов.

    Тег {\\k} перед словом задаёт время его подсветки в сантисекундах.
    Пауза между словами (от end предыдущего до start следующего) добавляется
    к тегу СЛЕДУЮЩЕГО слова, чтобы сумма тегов совпала с длиной всей строки.
    """
    if not words:
        return ""

    parts: list[str] = []
    for i, wt in enumerate(words):
        # Длительность самого слова
        word_dur_cs = int(round((wt.end - wt.start) * 100))

        # Пауза ПЕРЕД словом (от конца предыдущего до начала текущего)
        if i == 0:
            pause_cs = 0
        else:
            pause_cs = max(0, int(round((wt.start - words[i - 1].end) * 100)))

        k_val = word_dur_cs + pause_cs
        k_val = max(1, k_val)   # минимум 1 cs, чтобы тег был валиден
        parts.append(f"{{\\k{k_val}}}{wt.word}")

    return " ".join(parts)


# ─── Основная публичная функция ───────────────────────────────────────────────


def build_ass(
    words: list[WordTiming],
    out_path: Path,
    words_per_line: int = 3,
) -> Path:
    """
    Сгенерировать ASS-файл субтитров для вертикального видео.

    Args:
        words:          список WordTiming с глобальными таймингами.
        out_path:       путь для записи .ass (создаёт родительские директории).
        words_per_line: сколько слов объединять в одну строку (рекомендовано 2–3).

    Returns:
        out_path (для удобства чейнинга).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Заголовок ASS
    lines: list[str] = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        f"PlayResX: {_PLAY_RES_X}",
        f"PlayResY: {_PLAY_RES_Y}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        _STYLE_LINE,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # Разбиваем слова на группы по words_per_line
    if words:
        n = len(words)
        for group_start in range(0, n, words_per_line):
            group = words[group_start: group_start + words_per_line]
            start_time = _to_ass_time(group[0].start)
            end_time   = _to_ass_time(group[-1].end)
            text       = _karaoke_line(group)
            dialogue = (
                f"Dialogue: 0,{start_time},{end_time},Sub,,0,0,0,,{text}"
            )
            lines.append(dialogue)

    # Пустая строка в конце файла
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
