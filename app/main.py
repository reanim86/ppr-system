"""
Главный модуль Системы ППР (FastAPI-приложение).

Содержит:
- HTTP API (все эндпоинты начинаются с /api): станки, график ППР,
  отметки выполнения, статистика, экспорт в Excel, авторизация;
- отдачу веб-интерфейса (index.html и статические файлы из app/static);
- проверку прав: для изменения данных нужна роль не-«viewer»
  (инженер или руководитель).

Логика работы с базой вынесена в db.py, аутентификация — в auth.py.
"""

import io
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import change_password, create_token, get_user_by_token, verify_user
from .db import execute, init_db, query

# Папка с веб-интерфейсом (index.html, style.css, app.js)
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Создание приложения и инициализация базы данных (таблицы, учётки, демо-данные)
app = FastAPI(title="Система ППР", version="1.0.0")
init_db()

# Общая часть SQL-запроса: задачи вместе с названием станка.
# Дальше к ней приклеиваются WHERE/ORDER BY.
TASK_SELECT = """
SELECT t.*, m.name AS machine_name
FROM tasks t
JOIN machines m ON m.id = t.machine_id
"""


# ---------------------------------------------------------------------------
# Зависимости FastAPI (проверка авторизации и прав)
# ---------------------------------------------------------------------------
def current_user(request: Request):
    """
    Dependency: извлекает токен из заголовка Authorization и возвращает
    данные пользователя. Если токен отсутствует/невалиден — 401.
    """
    token = request.headers.get("Authorization", "")
    if token.startswith("Bearer "):
        token = token[7:]
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Не авторизован")
    return user


def require_write(user=Depends(current_user)):
    """
    Dependency для операций записи. Пропускает все роли, кроме 'viewer'
    (роль «только просмотр»). Таким образом инженер и руководитель
    имеют одинаковые полные права.
    """
    if user["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Недостаточно прав")
    return user


# ---------------------------------------------------------------------------
# Pydantic-модели: валидация тела запросов
# ---------------------------------------------------------------------------
class LoginData(BaseModel):
    """Тело POST /api/login."""
    username: str
    password: str


class PasswordData(BaseModel):
    """Новый пароль (от 4 до 64 символов)."""
    new_password: str = Field(min_length=4, max_length=64)


class MachineIn(BaseModel):
    """Станок: название, модель, расположение, примечания."""
    name: str
    model: str = ""
    location: str = ""
    notes: str = ""


class TaskIn(BaseModel):
    """Работа по графику: станок, название, описание, периодичность, дата."""
    machine_id: int
    title: str
    description: str = ""
    frequency_days: int = Field(30, ge=1, le=3650)
    due_date: str  # формат ГГГГ-ММ-ДД


class CompleteIn(BaseModel):
    """Отметка выполнения: процент (0–100) и комментарий."""
    percent: int = Field(100, ge=0, le=100)
    comment: str = ""


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------
@app.post("/api/login")
def login(data: LoginData):
    """Вход в систему: проверяет логин/пароль и возвращает токен + роль."""
    user = verify_user(data.username.strip(), data.password)
    if not user:
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    return {"token": create_token(user["username"]),
            "username": user["username"],
            "role": user["role"]}


@app.get("/api/me")
def me(user=Depends(current_user)):
    """Возвращает текущего пользователя (используется при восстановлении сессии)."""
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/password")
def set_password(data: PasswordData, user=Depends(current_user)):
    """Смена собственного пароля."""
    change_password(user["username"], data.new_password)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Станки
# ---------------------------------------------------------------------------
@app.get("/api/machines")
def list_machines(user=Depends(current_user)):
    """Список всех станков (доступен всем ролям)."""
    return query("SELECT * FROM machines ORDER BY name")


@app.post("/api/machines")
def create_machine(data: MachineIn, user=Depends(require_write)):
    """Добавление станка (инженер/руководитель)."""
    if not data.name.strip():
        raise HTTPException(status_code=422, detail="Название станка не может быть пустым")
    mid = execute(
        "INSERT INTO machines (name, model, location, notes) VALUES (?, ?, ?, ?)",
        (data.name.strip(), data.model.strip(), data.location.strip(), data.notes.strip()),
    )
    # Возвращаем созданный объект целиком
    return query("SELECT * FROM machines WHERE id = ?", (mid,), one=True)


@app.put("/api/machines/{mid}")
def update_machine(mid: int, data: MachineIn, user=Depends(require_write)):
    """Изменение станка по id."""
    if not data.name.strip():
        raise HTTPException(status_code=422, detail="Название станка не может быть пустым")
    execute(
        "UPDATE machines SET name = ?, model = ?, location = ?, notes = ? WHERE id = ?",
        (data.name.strip(), data.model.strip(), data.location.strip(), data.notes.strip(), mid),
    )
    return query("SELECT * FROM machines WHERE id = ?", (mid,), one=True)


@app.delete("/api/machines/{mid}")
def delete_machine(mid: int, user=Depends(require_write)):
    """Удаление станка. Работы и история удалятся каскадно (внешний ключ)."""
    execute("DELETE FROM machines WHERE id = ?", (mid,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# График ППР (работы)
# ---------------------------------------------------------------------------
@app.get("/api/tasks")
def list_tasks(user=Depends(current_user)):
    """Полный график работ, отсортирован по сроку выполнения."""
    return query(TASK_SELECT + "ORDER BY t.due_date, m.name")


@app.post("/api/tasks")
def create_task(data: TaskIn, user=Depends(require_write)):
    """Добавление работы в график ППР."""
    _validate_task(data)
    machine = query("SELECT id FROM machines WHERE id = ?", (data.machine_id,), one=True)
    if not machine:
        raise HTTPException(status_code=404, detail="Станок не найден")
    tid = execute(
        "INSERT INTO tasks (machine_id, title, description, frequency_days, due_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (data.machine_id, data.title.strip(), data.description.strip(),
         data.frequency_days, data.due_date, date.today().isoformat()),
    )
    return query(TASK_SELECT + "WHERE t.id = ?", (tid,), one=True)


@app.put("/api/tasks/{tid}")
def update_task(tid: int, data: TaskIn, user=Depends(require_write)):
    """Изменение работы по id."""
    _validate_task(data)
    task = query("SELECT id FROM tasks WHERE id = ?", (tid,), one=True)
    if not task:
        raise HTTPException(status_code=404, detail="Работа не найдена")
    execute(
        "UPDATE tasks SET machine_id = ?, title = ?, description = ?, frequency_days = ?, due_date = ? "
        "WHERE id = ?",
        (data.machine_id, data.title.strip(), data.description.strip(),
         data.frequency_days, data.due_date, tid),
    )
    return query(TASK_SELECT + "WHERE t.id = ?", (tid,), one=True)


@app.delete("/api/tasks/{tid}")
def delete_task(tid: int, user=Depends(require_write)):
    """Удаление работы вместе с её историей (каскадно)."""
    execute("DELETE FROM tasks WHERE id = ?", (tid,))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Сроки и выполнение
# ---------------------------------------------------------------------------
@app.get("/api/due")
def due_tasks(user=Depends(current_user)):
    """
    Разбивка задач по срочности относительно сегодняшней даты:
    - overdue — просроченные (срок меньше сегодняшнего);
    - today   — срок на сегодня;
    - upcoming — ближайшие 7 дней (включая завтра).
    Фронтенд опрашивает этот эндпоинт каждые 30 секунд
    и показывает уведомления.
    """
    today = date.today()
    soon = (today + timedelta(days=7)).isoformat()
    t = today.isoformat()
    overdue = query(TASK_SELECT + "WHERE t.due_date < ? ORDER BY t.due_date, m.name", (t,))
    today_list = query(TASK_SELECT + "WHERE t.due_date = ? ORDER BY m.name", (t,))
    upcoming = query(TASK_SELECT + "WHERE t.due_date > ? AND t.due_date <= ? ORDER BY t.due_date", (t, soon))
    return {"overdue": overdue, "today": today_list, "upcoming": upcoming}


@app.post("/api/tasks/{tid}/complete")
def complete_task(tid: int, data: CompleteIn, user=Depends(require_write)):
    """
    Отметка выполнения работы.

    Всегда добавляет запись в историю (worklogs) с процентом и комментарием.
    Если процент >= 100, работа считается закрытой и сдвигается график:
    следующая дата = max(текущий срок, сегодня) + периодичность.
    При частичном выполнении (например, 50%) срок не меняется — работа
    остаётся в списке «сегодня/просрочено» до полного закрытия.
    """
    task = query("SELECT * FROM tasks WHERE id = ?", (tid,), one=True)
    if not task:
        raise HTTPException(status_code=404, detail="Работа не найдена")
    today = date.today().isoformat()
    # В историю записываем также, какой пользователь сделал отметку
    execute(
        "INSERT INTO worklogs (task_id, done_date, percent, comment, user, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tid, today, data.percent, data.comment.strip(), user["username"], today),
    )
    if data.percent >= 100:
        from datetime import datetime  # импорт локально, чтобы не затенять date
        due = datetime.strptime(task["due_date"], "%Y-%m-%d").date()
        next_due = max(due, date.today()) + timedelta(days=task["frequency_days"])
        execute("UPDATE tasks SET due_date = ? WHERE id = ?", (next_due.isoformat(), tid))
    return query(TASK_SELECT + "WHERE t.id = ?", (tid,), one=True)


@app.get("/api/tasks/{tid}/logs")
def task_logs(tid: int, user=Depends(current_user)):
    """История выполнения конкретной работы (новые записи сверху)."""
    return query("SELECT * FROM worklogs WHERE task_id = ? ORDER BY done_date DESC, id DESC", (tid,))


# ---------------------------------------------------------------------------
# Статистика и экспорт
# ---------------------------------------------------------------------------
@app.get("/api/stats")
def stats(user=Depends(current_user)):
    """Счётчики для карточек на вкладке «Сегодня»."""
    today = date.today().isoformat()
    overdue = query("SELECT COUNT(*) AS n FROM tasks WHERE due_date < ?", (today,))[0]["n"]
    due_today = query("SELECT COUNT(*) AS n FROM tasks WHERE due_date = ?", (today,))[0]["n"]
    machines = query("SELECT COUNT(*) AS n FROM machines")[0]["n"]
    tasks = query("SELECT COUNT(*) AS n FROM tasks")[0]["n"]
    return {"overdue": overdue, "due_today": due_today, "machines": machines, "tasks": tasks}


def _status_label(due_date: str) -> str:
    """
    Текстовый статус работы для отчёта Excel:
    Просрочено / Сегодня / Скоро (в течение 7 дней) / В графике.
    """
    today = date.today().isoformat()
    soon = (date.today() + timedelta(days=7)).isoformat()
    if due_date < today:
        return "Просрочено"
    if due_date == today:
        return "Сегодня"
    if due_date <= soon:
        return "Скоро"
    return "В графике"


@app.get("/api/export.xlsx")
def export_excel(user=Depends(current_user)):
    """
    Формирование отчёта в Excel (openpyxl) с четырьмя листами:
    Сводка, Станки, График ППР, История выполнения.

    Файл собирается в памяти (BytesIO) и отдаётся как вложение
    с именем Отчет_ППР_<дата>.xlsx.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        raise HTTPException(status_code=500, detail="Не установлен openpyxl. Запустите setup.bat")

    today = date.today()
    today_iso = today.isoformat()
    soon_iso = (today + timedelta(days=7)).isoformat()

    # Данные для отчёта
    machines = query("SELECT * FROM machines ORDER BY name")
    tasks = query(TASK_SELECT + "ORDER BY t.due_date, m.name")
    logs = query(
        "SELECT w.*, m.name AS machine_name, t.title AS task_title "
        "FROM worklogs w JOIN tasks t ON t.id = w.task_id JOIN machines m ON m.id = t.machine_id "
        "ORDER BY w.done_date DESC, w.id DESC"
    )

    # Словарь "id работы -> последняя запись её истории"
    # (используется в листе «График ППР» для колонки «Последнее выполнение»)
    last_done = {}
    for row in logs:
        last_done.setdefault(row["task_id"], row)

    # Стили оформления шапки таблиц
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1C3A5A")
    thin = Alignment(vertical="center")

    def style_sheet(ws, headers, widths):
        """Оформляет шапку листа: жирный текст на цветной заливке, ширина колонок, заморозка строки."""
        for col, (h, w) in enumerate(zip(headers, widths), start=1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = thin
            ws.column_dimensions[cell.column_letter].width = w
        ws.freeze_panes = "A2"

    # Лист 1. Сводка
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Показатель", "Значение"])
    ws.append(["Дата отчёта", today.strftime("%d.%m.%Y")])
    ws.append(["Всего станков", len(machines)])
    ws.append(["Всего работ в графике", len(tasks)])
    ws.append(["Просрочено", sum(1 for t in tasks if t["due_date"] < today_iso)])
    ws.append(["Срок на сегодня", sum(1 for t in tasks if t["due_date"] == today_iso)])
    ws.append(["Ближайшие 7 дней", sum(1 for t in tasks if today_iso < t["due_date"] <= soon_iso)])
    ws.append(["Выполнений всего", len(logs)])
    for row in ws.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 28

    # Лист 2. Станки
    ws = wb.create_sheet("Станки")
    style_sheet(ws, ["№", "Название", "Модель", "Расположение", "Примечания", "Работ в графике"], (6, 32, 16, 20, 34, 16))
    for i, m in enumerate(machines, start=1):
        n = sum(1 for t in tasks if t["machine_id"] == m["id"])
        ws.append([i, m["name"], m["model"], m["location"], m["notes"], n])

    # Лист 3. График ППР
    ws = wb.create_sheet("График ППР")
    style_sheet(ws, ["Станок", "Работа", "Описание", "Периодичность, дн.",
                     "Следующий срок", "Статус", "Последнее выполнение",
                     "Последний результат", "Кто выполнил"],
                (32, 26, 36, 16, 14, 14, 14, 18, 16))
    for t in tasks:
        last = last_done.get(t["id"])
        ws.append([
            t["machine_name"], t["title"], t["description"], t["frequency_days"],
            t["due_date"], _status_label(t["due_date"]),
            last["done_date"] if last else "",
            f"{last['percent']}%" if last else "",
            last["user"] if last else "",
        ])

    # Лист 4. История выполнения
    ws = wb.create_sheet("История выполнения")
    style_sheet(ws, ["Дата", "Станок", "Работа", "Кто", "Результат", "Комментарий"],
                (14, 32, 30, 14, 10, 40))
    for w in logs:
        ws.append([w["done_date"], w["machine_name"], w["task_title"], w["user"],
                   f"{w['percent']}%", w["comment"]])

    # Сохраняем книгу в память и отдаём как файл
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    # Имя файла с кириллицей: добавляем и ASCII-вариант, и RFC 5987 filename*=
    # (чтобы корректно скачивалось во всех браузерах)
    filename = f"Отчет_ППР_{today_iso}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=\"ppr_report_{today_iso}.xlsx\"; "
                                   f"filename*=UTF-8''{quote(filename)}"
        },
    )


def _validate_task(data: TaskIn):
    """
    Проверка данных работы перед записью:
    - дата должна быть в формате ГГГГ-ММ-ДД;
    - название работы не может быть пустым.
    """
    try:
        date.fromisoformat(data.due_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Неверный формат даты (нужен ГГГГ-ММ-ДД)")
    if not data.title.strip():
        raise HTTPException(status_code=422, detail="Название работы не может быть пустым")


# ---------------------------------------------------------------------------
# Веб-интерфейс
# ---------------------------------------------------------------------------
# Статические файлы (style.css, app.js) из app/static
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    """Главная страница — файл веб-интерфейса index.html."""
    return FileResponse(STATIC_DIR / "index.html")