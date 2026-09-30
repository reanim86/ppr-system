"""
Работа с базой данных (SQLite).

Здесь определены:
- путь к файлу БД (ppr.db рядом с проектом, либо заданный через PPR_DB);
- схема всех таблиц (пользователи, станки, работы, история выполнения);
- инициализация БД при первом запуске (создание таблиц, учётных записей
  по умолчанию и демо-данных);
- универсальные функции query() и execute() для чтения и записи.

Все даты хранятся в текстовом формате ISO (ГГГГ-ММ-ДД) — так их удобно
сравнивать между собой и с сегодняшней датой.
"""

import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

# Корень проекта = родительская папка каталога app/
BASE_DIR = Path(__file__).resolve().parent.parent

# Файл БД. По умолчанию — ppr.db в корне проекта.
# Можно переопределить переменной окружения PPR_DB (используется на Ubuntu-сервере).
DB_PATH = os.environ.get("PPR_DB", str(BASE_DIR / "ppr.db"))


def hash_password(password: str, salt: str) -> str:
    """
    Хеширование пароля алгоритмом PBKDF2-HMAC-SHA256 со 100 000 итераций.

    В качестве "соли" используется имя пользователя — это упрощает схему,
    но для продакшена лучше генерировать случайную соль на каждого пользователя.
    В БД хранится только хеш, никогда не сам пароль.
    """
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100_000).hex()


def get_conn():
    """
    Создаёт новое соединение с БД.

    - row_factory: возвращать строки как словари (обращение по имени колонки);
    - PRAGMA foreign_keys = ON: включаем внешние ключи, чтобы работало
      каскадное удаление (удалили станок — удалились его работы).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Схема базы данных. Создаётся командой executescript при инициализации.
SCHEMA = """
-- Пользователи системы: engineer (инженер), admin (руководитель), viewer (просмотр).
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'engineer'
);

-- Станки (оборудование), по которым ведётся график ППР.
CREATE TABLE IF NOT EXISTS machines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT ''
);

-- Работы по графику ППР.
-- machine_id    — станок, к которому относится работа;
-- frequency_days — периодичность в днях (30 = ежемесячно, 90 = ежеквартально);
-- due_date      — дата следующего выполнения; после отметки 100%
--                 сдвигается на frequency_days вперёд.
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    frequency_days INTEGER NOT NULL DEFAULT 30,
    due_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- История выполнения: одна строка на каждую отметку (частичную или полную).
-- percent = 100 означает, что работа закрыта и график сдвинулся.
-- user — логин пользователя, который сделал отметку о выполнении.
CREATE TABLE IF NOT EXISTS worklogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    done_date TEXT NOT NULL,
    percent INTEGER NOT NULL DEFAULT 100,
    comment TEXT NOT NULL DEFAULT '',
    user TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Индексы для быстрого поиска "что просрочено" и истории по работе.
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_date);
CREATE INDEX IF NOT EXISTS idx_worklogs_task ON worklogs(task_id);
"""


def init_db():
    """
    Инициализация базы данных. Вызывается один раз при старте приложения.

    - создаёт таблицы, если их ещё нет;
    - добавляет недостающие колонки в существующие таблицы (миграции);
    - при пустой таблице users добавляет учётные записи по умолчанию
      (engineer/engineer123 — инженер, boss/boss123 — руководитель с полными правами);
    - если пользователи уже есть — обновляет роль boss до 'admin'
      (миграция со старых версий, где руководитель был только читателем);
    - при отсутствии станков добавляет демо-данные, чтобы система
      сразу была наглядно наполнена.
    """
    with closing(get_conn()) as conn:
        conn.executescript(SCHEMA)

        # Миграция для старых баз: добавляем колонку user (кто отметил выполнение),
        # если она ещё не существует (CREATE TABLE IF NOT EXISTS новые колонки не добавляет)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(worklogs)")]
        if "user" not in cols:
            conn.execute("ALTER TABLE worklogs ADD COLUMN user TEXT NOT NULL DEFAULT ''")

        # Учётные записи по умолчанию (только при самом первом запуске)
        cur = conn.execute("SELECT COUNT(*) AS n FROM users")
        if cur.fetchone()["n"] == 0:
            for username, password, role in (("engineer", "engineer123", "engineer"),
                                             ("boss", "boss123", "admin")):
                conn.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                    (username, hash_password(password, username), role),
                )
        else:
            # Миграция старых баз: руководитель получает полные права
            conn.execute("UPDATE users SET role = 'admin' WHERE username = 'boss' AND role = 'viewer'")

        # Демо-данные только при пустой таблице станков
        cur = conn.execute("SELECT COUNT(*) AS n FROM machines")
        if cur.fetchone()["n"] == 0:
            _seed_sample(conn)

        conn.commit()


def _seed_sample(conn):
    """
    Наполняет БД демо-данными: два станка и по несколько типовых работ.
    Создано, чтобы после установки сразу было видно, как работает система.
    Удалить можно из веб-интерфейса (вкладки «Станки» и «График ППР»).
    """
    today = date.today().isoformat()

    # Демо-станки
    machines = (
        ("Токарный станок Т-1", "16К20", "Цех 1"),
        ("Фрезерный станок Ф-1", "6Р13", "Цех 1"),
    )
    for name, model, loc in machines:
        conn.execute("INSERT INTO machines (name, model, location) VALUES (?, ?, ?)", (name, model, loc))

    # Их идентификаторы (первый и последний добавленные)
    cur = conn.execute("SELECT id FROM machines ORDER BY id LIMIT 1")
    m1 = cur.fetchone()["id"]
    cur = conn.execute("SELECT id FROM machines ORDER BY id DESC LIMIT 1")
    m2 = cur.fetchone()["id"]

    # Демо-работы (срок — сегодня, чтобы сразу были видны уведомления)
    sample = (
        (m1, "Замена масла", "Замена масла в редукторе", 90, today),
        (m1, "Проверка подшипников", "Осмотр и смазка подшипников шпинделя", 30, today),
        (m2, "Контроль режущего инструмента", "Осмотр и замена фрезы при износе", 180, today),
    )
    for machine_id, title, desc, freq, due in sample:
        conn.execute(
            "INSERT INTO tasks (machine_id, title, description, frequency_days, due_date, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (machine_id, title, desc, freq, due, today),
        )


def query(sql, params=(), one=False):
    """
    Универсальная функция чтения из БД.

    :param sql:    SQL-запрос (SELECT);
    :param params: параметры для подстановки вместо '?' (защита от SQL-инъекций);
    :param one:    True — вернуть одну строку (словарь) или None,
                   False — вернуть список словарей.
    """
    with closing(get_conn()) as conn:
        cur = conn.execute(sql, params)
        if one:
            row = cur.fetchone()
            return dict(row) if row else None
        return [dict(r) for r in cur.fetchall()]


def execute(sql, params=()):
    """
    Универсальная функция записи в БД (INSERT/UPDATE/DELETE).

    Сразу фиксирует транзакцию (commit) и возвращает id последней
    вставленной строки (полезно после INSERT, чтобы вернуть созданный объект).
    """
    with closing(get_conn()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid