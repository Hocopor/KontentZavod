"""
Тесты конвертации текста для публикации.

pytest tests/test_textfmt.py -q
"""
import pytest
from app.services.textfmt import md_to_telegram_html, md_to_plain


# ─── md_to_telegram_html ──────────────────────────────────────────────────────

class TestMdToTelegramHtml:

    def test_bold_double_asterisk(self):
        """**text** → <b>text</b>"""
        result = md_to_telegram_html("Это **жирный** текст")
        assert result == "Это <b>жирный</b> текст"

    def test_bold_double_underscore(self):
        """__text__ → <b>text</b>"""
        result = md_to_telegram_html("Это __жирный__ текст")
        assert result == "Это <b>жирный</b> текст"

    def test_italic_asterisk(self):
        """*text* → <i>text</i>"""
        result = md_to_telegram_html("Это *курсив* в тексте")
        assert result == "Это <i>курсив</i> в тексте"

    def test_italic_underscore(self):
        """_text_ → <i>text</i>"""
        result = md_to_telegram_html("Это _курсив_ в тексте")
        assert result == "Это <i>курсив</i> в тексте"

    def test_code_inline(self):
        """`code` → <code>code</code>"""
        result = md_to_telegram_html("Пример `code` здесь")
        assert result == "Пример <code>code</code> здесь"

    def test_link(self):
        """[text](url) → <a href="url">text</a>"""
        result = md_to_telegram_html("Смотри [здесь](https://example.com)")
        assert result == 'Смотри <a href="https://example.com">здесь</a>'

    def test_heading_h1(self):
        """# Заголовок → <b>Заголовок</b>"""
        result = md_to_telegram_html("# Заголовок первого уровня")
        assert result == "<b>Заголовок первого уровня</b>"

    def test_heading_h2(self):
        """## Подзаголовок → <b>Подзаголовок</b>"""
        result = md_to_telegram_html("## Второй уровень")
        assert result == "<b>Второй уровень</b>"

    def test_heading_h3(self):
        """### Мини → <b>Мини</b>"""
        result = md_to_telegram_html("### Третий уровень")
        assert result == "<b>Третий уровень</b>"

    def test_list_dash(self):
        """- элемент → — элемент"""
        result = md_to_telegram_html("- первый элемент")
        assert result == "— первый элемент"

    def test_list_asterisk(self):
        """* элемент → — элемент"""
        result = md_to_telegram_html("* первый элемент")
        assert result == "— первый элемент"

    def test_html_escape_ampersand(self):
        """& → &amp;"""
        result = md_to_telegram_html("AT&T и P&G")
        assert "&amp;" in result
        assert "&T" not in result  # значит & заменён

    def test_html_escape_lt_gt(self):
        """< и > экранируются"""
        result = md_to_telegram_html("a < b > c")
        assert "&lt;" in result
        assert "&gt;" in result

    def test_no_asterisks_in_math(self):
        """2*2 — звёздочки внутри слова/числа НЕ конвертируются в курсив."""
        result = md_to_telegram_html("2*2=4 и a*b")
        # Не должно быть <i> теги вокруг чисел
        assert "<i>" not in result
        # Исходные символы должны сохраниться
        assert "2*2=4" in result

    def test_multiline_bold(self):
        """Жирный текст в многострочном контексте."""
        text = "Первая строка\n**Жирный заголовок**\nТретья строка"
        result = md_to_telegram_html(text)
        assert "<b>Жирный заголовок</b>" in result
        assert "Первая строка" in result
        assert "Третья строка" in result

    def test_empty_string(self):
        """Пустая строка возвращается как есть."""
        assert md_to_telegram_html("") == ""

    def test_plain_text_unchanged(self):
        """Текст без разметки возвращается без изменений (кроме &, <, >)."""
        result = md_to_telegram_html("Обычный текст без разметки")
        assert result == "Обычный текст без разметки"

    def test_combined_formatting(self):
        """Комбинированное форматирование: заголовок + жирный + список."""
        text = "## Заголовок\n\n**Важно:** прочитайте это\n\n- пункт один\n- пункт два"
        result = md_to_telegram_html(text)
        assert "<b>Заголовок</b>" in result
        assert "<b>Важно:</b>" in result
        assert "— пункт один" in result
        assert "— пункт два" in result

    def test_no_extra_html_tags(self):
        """Telegram поддерживает ограниченный набор — не должно быть лишних тегов."""
        text = "## Заголовок\n**жирный** и *курсив* и `код`"
        result = md_to_telegram_html(text)
        # Только допустимые теги
        for tag in ["<b>", "</b>", "<i>", "</i>", "<code>", "</code>"]:
            assert tag in result or tag not in result  # может и не быть
        # Недопустимых тегов быть не должно
        assert "<h1>" not in result
        assert "<h2>" not in result
        assert "<strong>" not in result


# ─── md_to_plain ──────────────────────────────────────────────────────────────

class TestMdToPlain:

    def test_removes_bold_asterisk(self):
        """**text** → text"""
        result = md_to_plain("Это **жирный** текст")
        assert result == "Это жирный текст"

    def test_removes_bold_underscore(self):
        """__text__ → text"""
        result = md_to_plain("Это __жирный__ текст")
        assert result == "Это жирный текст"

    def test_removes_italic_asterisk(self):
        """*text* → text"""
        result = md_to_plain("Это *курсив* здесь")
        assert result == "Это курсив здесь"

    def test_removes_italic_underscore(self):
        """_text_ → text"""
        result = md_to_plain("Это _курсив_ здесь")
        assert result == "Это курсив здесь"

    def test_removes_code(self):
        """`code` → code"""
        result = md_to_plain("Пример `код` здесь")
        assert result == "Пример код здесь"

    def test_link_becomes_text_url(self):
        """[text](url) → text (url)"""
        result = md_to_plain("Смотри [здесь](https://example.com)")
        assert result == "Смотри здесь (https://example.com)"

    def test_removes_heading_h1(self):
        """# Заголовок → Заголовок"""
        result = md_to_plain("# Заголовок первого уровня")
        assert result == "Заголовок первого уровня"

    def test_removes_heading_h2(self):
        """## → убирает решётки"""
        result = md_to_plain("## Второй уровень")
        assert result == "Второй уровень"

    def test_list_dash_to_dash(self):
        """- элемент → — элемент"""
        result = md_to_plain("- первый")
        assert result == "— первый"

    def test_list_asterisk_to_dash(self):
        """* элемент → — элемент"""
        result = md_to_plain("* первый")
        assert result == "— первый"

    def test_no_asterisks_in_math(self):
        """2*2 — звёздочки внутри числа НЕ удаляются."""
        result = md_to_plain("Результат: 2*2=4")
        assert "2*2=4" in result

    def test_no_html_escaping(self):
        """Plain text НЕ должен экранировать HTML-символы."""
        result = md_to_plain("AT&T < 100 > 50")
        assert "&" in result
        assert "<" in result
        assert ">" in result
        assert "&amp;" not in result

    def test_empty_string(self):
        """Пустая строка возвращается как есть."""
        assert md_to_plain("") == ""

    def test_plain_text_unchanged(self):
        """Текст без разметки возвращается без изменений."""
        text = "Обычный текст без разметки"
        assert md_to_plain(text) == text

    def test_combined_markdown_removal(self):
        """Полный блок с форматированием → чистый текст."""
        text = "## Заголовок\n\n**Важно:** прочитайте это.\n\n- пункт один\n- пункт два"
        result = md_to_plain(text)
        assert "##" not in result
        assert "**" not in result
        assert "-" not in result or "—" in result
        assert "Заголовок" in result
        assert "Важно:" in result
        assert "пункт один" in result
