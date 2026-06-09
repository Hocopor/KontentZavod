"""
Шифрование/дешифрование токенов площадок (Fernet, симметричное AES-128-CBC + HMAC).

Ключ берётся из FERNET_KEY в .env.
Зашифрованные значения хранятся в project_platforms.credentials.

Использование:
    from app.security import encrypt, decrypt

    encrypted = encrypt('{"token": "abc123"}')
    original  = decrypt(encrypted)
"""
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _get_fernet() -> Fernet:
    key = settings.FERNET_KEY
    if not key:
        raise RuntimeError(
            "FERNET_KEY не задан в .env — задайте его перед запуском приложения."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    """
    Зашифровать строку (JSON с токенами).
    Возвращает base64-строку пригодную для хранения в TEXT-поле SQLite.
    """
    f = _get_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """
    Расшифровать строку.
    Поднимает ValueError при невалидном/повреждённом шифротексте.
    """
    f = _get_fernet()
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Не удалось расшифровать данные: токен повреждён или ключ неверный.") from exc
