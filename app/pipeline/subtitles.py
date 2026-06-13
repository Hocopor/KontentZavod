r"""
Генерация ASS-субтитров для вертикальных видео 1080×1920.

Публичный API:
    build_ass(words, out_path, words_per_line=3,
              font_color="#ffffff", outline_color="#000000",
              outline_width=5, highlight_color="#ffe600",
              time_offset=0.0) -> Path

Особенности:
  - PlayResX=1080, PlayResY=1920 (вертикальный формат).
  - Стиль «Sub»: Arial 110 Bold, белый основной цвет.
  - TikTok-стиль накопительного появления: на каждое слово — отдельный
    Dialogue. Событие k показывает слова [0..k], слово k подсвечено
    жёлтым inline-тегом {\\1c}, прошлые — обычным белым.
  - time_offset (сек) прибавляется ко всем таймингам (подстройка синхрона).
  - Блок текста выровнен по нижней трети экрана (MarginV=550),
    не перекрывает UI Shorts/Reels.
"""
from pathlib import Path

from app.pipeline.tts import WordTiming

# ─── Константы стиля ─────────────────────────────────────────────────────────

_PLAY_RES_X = 1080
_PLAY_RES_Y = 1920

# Дефолтные цвета (используются если kwargs не переданы)
_DEFAULT_FONT_COLOR      = "#ffffff"   # белый шрифт
_DEFAULT_OUTLINE_COLOR   = "#000000"   # чёрное обрамление
_DEFAULT_OUTLINE_WIDTH   = 5
_DEFAULT_HIGHLIGHT_COLOR = "#ffe600"   # жёлтая подсветка текущего слова

# Хвост строки после последнего слова (сек) — задержка исчезновения
_TAIL: float = 0.5


# ─── Хелперы цветов ──────────────────────────────────────────────────────────


def _hex_to_ass(hex_color: str, alpha: int = 0x00) -> str:
    """
    Конвертировать HEX-цвет (#RRGGBB) в формат ASS (&HAABBGGRR).

    Args:
        hex_color: строка вида "#rrggbb" (регистр не важен).
        alpha:     значение прозрачности 0x00 (непрозрачный) … 0xFF (невидимый).

    Returns:
        Строка вида "&HAABBGGRR".
    """
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _hex_to_ass_inline(hex_color: str) -> str:
    """HEX (#rrggbb) → 6-значный inline-цвет ASS '&HBBGGRR&' для тега \\1c."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _build_style_line(
    font_color: str,
    outline_color: str,
    outline_width: int,
    highlight_color: str,
) -> str:
    """
    Сформировать строку Style для секции [V4+ Styles].

    Накопительный стиль (без karaoke):
        PrimaryColour   = font_color   (белый — основной/резерв, прошлые слова)
        SecondaryColour = font_color   (тоже font; karaoke не используем)
        OutlineColour   = outline_color
        BackColour      = outline_color с alpha=0x80 (тень)

    Подсветка текущего слова — через inline-тег {\\1c...} в тексте Dialogue.
    """
    primary   = _hex_to_ass(font_color, 0x00)      # прошлые слова — белый
    secondary = _hex_to_ass(font_color, 0x00)       # тоже font (karaoke не используем)
    outline   = _hex_to_ass(outline_color, 0x00)
    back      = _hex_to_ass(outline_color, 0x80)    # тень с прозрачностью

    return (
        "Style: Sub,"           # Имя стиля
        "Arial,"                # Fontname
        "110,"                  # Fontsize
        f"{primary},"           # PrimaryColour   — основной (прошлые слова)
        f"{secondary},"         # SecondaryColour — тоже font
        f"{outline},"           # OutlineColour
        f"{back},"              # BackColour (тень)
        "-1,"                   # Bold (-1 = True в ASS)
        "0,"                    # Italic
        "0,"                    # Underline
        "0,"                    # StrikeOut
        "100,"                  # ScaleX
        "100,"                  # ScaleY
        "0,"                    # Spacing
        "0,"                    # Angle
        "1,"                    # BorderStyle (outline+shadow)
        f"{outline_width},"     # Outline
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


def _escape_text(s: str) -> str:
    """
    Экранировать текст слова для ASS: фигурные скобки → круглые
    (не сломать inline-теги), переводы строк → пробел.
    """
    s = s.replace("{", "(").replace("}", ")")
    s = s.replace("\n", " ")
    return s


# ─── Основная публичная функция ───────────────────────────────────────────────


def build_ass(
    words: list[WordTiming],
    out_path: Path,
    words_per_line: int = 3,
    font_color: str = _DEFAULT_FONT_COLOR,
    outline_color: str = _DEFAULT_OUTLINE_COLOR,
    outline_width: int = _DEFAULT_OUTLINE_WIDTH,
    highlight_color: str = _DEFAULT_HIGHLIGHT_COLOR,
    time_offset: float = 0.0,
) -> Path:
    """
    Сгенерировать ASS-файл субтитров с накопительным пословным появлением.

    TikTok-стиль: на каждое слово — отдельный Dialogue. Событие k показывает
    слова [0..k] текущей строки, слово k подсвечено жёлтым, предыдущие — белые.
    Будущие слова не видны (накопление).

    Args:
        words:           список WordTiming с глобальными таймингами.
        out_path:        путь для записи .ass (создаёт родительские директории).
        words_per_line:  сколько слов в одной строке (рекомендовано 2–3).
        font_color:      HEX-цвет основного текста ("#rrggbb"), по умолчанию белый.
        outline_color:   HEX-цвет обрамления/тени, по умолчанию чёрный.
        outline_width:   толщина обрамления в пикселях (1–10), по умолчанию 5.
        highlight_color: HEX-цвет подсветки текущего слова, по умолчанию жёлтый.
        time_offset:     сдвиг таймингов в секундах (>0 — субтитры позже).

    Returns:
        out_path (для удобства чейнинга).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    style_line = _build_style_line(font_color, outline_color, outline_width, highlight_color)

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
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        style_line,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    if words:
        # Применить смещение
        shifted = [
            (w.word, max(0.0, w.start + time_offset), max(0.0, w.end + time_offset))
            for w in words
        ]

        hl = _hex_to_ass_inline(highlight_color)

        # Разбить на группы по words_per_line
        n = len(shifted)
        groups: list[list[tuple[str, float, float]]] = []
        for gi in range(0, n, words_per_line):
            groups.append(shifted[gi: gi + words_per_line])

        for gi, group in enumerate(groups):
            L = len(group)

            # Время исчезновения строки
            if gi < len(groups) - 1:
                line_end = groups[gi + 1][0][1]  # start первого слова следующей группы
            else:
                line_end = group[-1][2] + _TAIL   # end последнего слова + хвост

            for k in range(L):
                w_word, w_start, _ = group[k]

                if k < L - 1:
                    w_end = group[k + 1][1]   # start следующего слова в группе
                else:
                    w_end = line_end

                w_end = max(w_end, w_start + 0.01)  # гарантия end > start

                # Текст: слова [0..k-1] обычным цветом, слово k — жёлтым inline
                parts: list[str] = []
                for j in range(k):
                    parts.append(_escape_text(group[j][0]))
                parts.append("{\\1c" + hl + "}" + _escape_text(w_word))
                text = " ".join(parts)

                dialogue = (
                    f"Dialogue: 0,{_to_ass_time(w_start)},{_to_ass_time(w_end)},"
                    f"Sub,,0,0,0,,{text}"
                )
                lines.append(dialogue)

    # Пустая строка в конце файла
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
