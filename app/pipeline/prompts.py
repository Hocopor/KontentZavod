"""
Загрузчик шаблонов промптов из app/prompts/*.txt.

Использует плейсхолдеры вида <<VAR>> вместо {VAR}, чтобы безопасно
работать с JSON-примерами внутри самих шаблонов (фигурные скобки в
шаблоне не конфликтуют с str.format).
"""
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def load_prompt(name: str, **kwargs: str) -> str:
    """
    Загрузить шаблон <name>.txt и подставить значения.

    Args:
        name:    имя файла без расширения ('ideas', 'script')
        **kwargs: пары VAR=значение для подстановки <<VAR>>

    Returns:
        Готовая строка промпта с подставленными значениями.

    Raises:
        FileNotFoundError: если файл шаблона не найден.
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    template = path.read_text(encoding="utf-8")
    for key, value in kwargs.items():
        template = template.replace(f"<<{key}>>", value or "")
    return template
