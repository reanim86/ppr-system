"""
Аутентификация и авторизация.

Схема работы:
- пользователь отправляет логин/пароль на POST /api/login;
- verify_user() проверяет пароль и в случае успеха create_token()
  выдаёт случайный токен;
- токен хранится в памяти сервера (словарь _tokens) и передаётся
  клиентом в заголовке "Authorization: Bearer <токен>";
- при каждом запросе get_user_by_token() по токену находит пользователя
  в базе и возвращает его данные (включая роль).

Особенности:
- токены живут только пока запущен сервер: после перезапуска все
  пользователи должны войти заново (для локальной системы это нормально);
- доступ к _tokens защищён блокировкой threading.Lock, потому что
  FastAPI/uvicorn обрабатывает запросы в разных потоках.
"""

import secrets
import threading

from .db import hash_password, query

# Словарь "токен -> имя пользователя". Хранится в памяти процесса.
_tokens = {}
# Защита словаря токенов при одновременных запросах из разных потоков
_lock = threading.Lock()


def verify_user(username: str, password: str):
    """
    Проверка логина и пароля.

    Ищет пользователя по имени, сравнивает хеш введённого пароля
    с хешем из базы. Возвращает строку пользователя (словарь) при успехе
    или None, если имя не найдено / пароль неверный.
    """
    user = query("SELECT * FROM users WHERE username = ?", (username,), one=True)
    if user and user["password_hash"] == hash_password(password, username):
        return user
    return None


def create_token(username: str) -> str:
    """
    Создаёт новый случайный токен для пользователя.

    Токен — 32 случайных байта в hex (64 символа), сгенерированных
    криптостойким генератором secrets. Привязывает токен к имени пользователя.
    """
    token = secrets.token_hex(32)
    with _lock:
        _tokens[token] = username
    return token


def get_user_by_token(token: str):
    """
    Возвращает данные пользователя по токену (или None, если токен невалиден).

    Используется для проверки авторизации в каждом запросе,
    требующем входа в систему.
    """
    with _lock:
        username = _tokens.get(token)
    if not username:
        return None
    return query("SELECT * FROM users WHERE username = ?", (username,), one=True)


def change_password(username: str, new_password: str) -> bool:
    """
    Смена пароля: пересчитывает хеш и обновляет его в базе.

    Возвращает True при успехе. Импорт execute() сделан локально,
    чтобы избежать циклического импорта (db не импортирует auth).
    """
    from .db import execute
    row = execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (hash_password(new_password, username), username),
    )
    return row > 0