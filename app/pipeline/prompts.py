"""
Загрузчик шаблонов промптов из app/prompts/*.txt.

Использует плейсхолдеры вида <<VAR>> вместо {VAR}, чтобы безопасно
работать с JSON-примерами внутри самих шаблонов (фигурные скобки в
шаблоне не конфликтуют с str.format).
"""
from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

# Кеш базовой части правил (AGENTS.md + forbidden_ru.md) — файлы статичны,
# загружаются один раз. Сбросить в тестах: prompts._rules_cache = None
_rules_cache: str | None = None


def load_rules(project_settings: dict | None = None) -> str:
    """
    Загрузить правила маркетингового мозга и вернуть итоговую строку.

    Читает AGENTS.md и forbidden_ru.md из app/prompts/agents/ и конкатенирует.
    Отсутствующие файлы не вызывают исключение — просто пропускаются.
    Базовая часть кешируется в _rules_cache; project-специфичная часть
    (project_settings["agent_rules"]) добавляется при каждом вызове.

    Args:
        project_settings: словарь настроек проекта (из get_project_settings).
                          Если содержит непустой ключ "agent_rules" — его текст
                          добавляется в конец как раздел «Дополнительные правила проекта».

    Returns:
        Готовая строка правил для вставки в системный промпт.
    """
    global _rules_cache

    if _rules_cache is None:
        agents_dir = _PROMPTS_DIR / "agents"
        parts: list[str] = []
        for filename in ("AGENTS.md", "forbidden_ru.md"):
            try:
                parts.append((agents_dir / filename).read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
        _rules_cache = "\n\n".join(parts)

    result = _rules_cache

    # Добавляем project-специфичные правила поверх кеша
    if project_settings:
        extra = project_settings.get("agent_rules")
        if extra and isinstance(extra, str) and extra.strip():
            result = result + "\n\n## Дополнительные правила проекта\n" + extra

    return result


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
