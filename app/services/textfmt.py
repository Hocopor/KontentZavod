"""
Форматирование текста под конкретные платформы публикации.

Функции конвертируют Markdown-разметку (которую генерирует LLM) в формат,
пригодный для публикации на каждой платформе.

md_to_telegram_html(text) -> str
    Конвертирует простой Markdown в HTML для Telegram (parse_mode=HTML).
    Telegram поддерживает ограниченный набор тегов: <b>, <i>, <code>, <a>.

md_to_plain(text) -> str
    Убирает всю Markdown-разметку, возвращает чистый текст.
    Используется для VK, Instagram, Яндекс.Дзен.

Регулярки аккуратны:
  - не ломают звёздочки внутри слов (2*2, пример*)
  - корректно обрабатывают многострочный текст
  - сначала парсинг жирного (**x**), потом курсива (*x*) — избегаем конфликтов
"""
import html
import re


# ─── Вспомогательные регулярки ────────────────────────────────────────────────

# Жирный: **text** или __text__ (пробел или начало/конец строки вокруг маркера)
_BOLD_RE = re.compile(r'(?<!\w)(\*\*|__)(.+?)(\*\*|__)(?!\w)', re.DOTALL)

# Курсив: *text* или _text_ (только слово-граница вокруг маркера, не звёздочки в числах)
# Используем (?<!\*) и (?!\*) чтобы не цеплять остатки **bold**
_ITALIC_RE = re.compile(r'(?<!\w)(?<!\*)(\*|_)(?!\s)(.+?)(?<!\s)(\*|_)(?!\w)(?!\*)', re.DOTALL)

# Inline code: `text`
_CODE_RE = re.compile(r'`([^`]+)`')

# Ссылки: [text](url)
_LINK_RE = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

# Заголовки: # / ## / ### в начале строки (до 3 уровней)
_HEADING_RE = re.compile(r'^#{1,3}\s+(.+)$', re.MULTILINE)

# Маркеры списков: - или * в начале строки (с опциональным отступом)
_LIST_MARKER_RE = re.compile(r'^[ \t]*[-*]\s+', re.MULTILINE)


# ─── Telegram HTML ────────────────────────────────────────────────────────────


def md_to_telegram_html(text: str) -> str:
    """
    Конвертировать Markdown-текст в HTML для Telegram (parse_mode=HTML).

    Порядок обработки важен:
      1. Экранировать HTML-спецсимволы (&, <, >) — до любых замен тегами.
      2. Заменить заголовки → <b>строка</b>.
      3. Заменить **bold** / __bold__ → <b>…</b>.
      4. Заменить *italic* / _italic_ → <i>…</i>.
      5. Заменить `code` → <code>…</code>.
      6. Заменить [text](url) → <a href="url">text</a>.
      7. Маркеры списков → — .

    Telegram поддерживает: <b> <strong> <i> <em> <u> <ins> <s> <strike>
    <del> <tg-spoiler> <a> <code> <pre>. Остальные — НЕ добавлять.
    """
    if not text:
        return text

    # 1. Экранируем HTML-спецсимволы (не трогаем наши будущие теги)
    result = html.escape(text, quote=False)

    # 2. Заголовки → <b>строка</b>
    result = _HEADING_RE.sub(lambda m: f"<b>{m.group(1)}</b>", result)

    # 3. Жирный **text** / __text__ → <b>text</b>
    result = _BOLD_RE.sub(lambda m: f"<b>{m.group(2)}</b>", result)

    # 4. Курсив *text* / _text_ → <i>text</i>
    result = _ITALIC_RE.sub(lambda m: f"<i>{m.group(2)}</i>", result)

    # 5. Inline code → <code>text</code>
    result = _CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", result)

    # 6. Ссылки [text](url) → <a href="url">text</a>
    result = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', result)

    # 7. Маркеры списков → — (тире + пробел)
    result = _LIST_MARKER_RE.sub("— ", result)

    return result


# ─── Plain text (VK, Instagram, Дзен) ────────────────────────────────────────


def md_to_plain(text: str) -> str:
    """
    Убрать всю Markdown-разметку, вернуть чистый текст.

    Порядок:
      1. Заголовки # ## ### → просто текст строки.
      2. Жирный **text** / __text__ → text.
      3. Курсив *text* / _text_ → text.
      4. Inline code `text` → text.
      5. Ссылки [text](url) → text (url).
      6. Маркеры списков → — .
    """
    if not text:
        return text

    result = text

    # 1. Заголовки
    result = _HEADING_RE.sub(lambda m: m.group(1), result)

    # 2. Жирный
    result = _BOLD_RE.sub(lambda m: m.group(2), result)

    # 3. Курсив
    result = _ITALIC_RE.sub(lambda m: m.group(2), result)

    # 4. Inline code
    result = _CODE_RE.sub(lambda m: m.group(1), result)

    # 5. Ссылки [text](url) → text (url)
    result = _LINK_RE.sub(lambda m: f"{m.group(1)} ({m.group(2)})", result)

    # 6. Маркеры списков
    result = _LIST_MARKER_RE.sub("— ", result)

    return result
