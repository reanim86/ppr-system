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

# Общая часть SQL-запроса: задачи вместе с названием станка и его моделью.
# Дальше к ней приклеиваются WHERE/ORDER BY.
TASK_SELECT = """
SELECT t.*, m.name AS machine_name, m.model AS machine_model
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
    """Оборудование: название, модель, мощность, расположение, примечания."""
    name: str
    model: str = ""
    power: str = ""
    location: str = ""
    notes: str = ""


class TaskIn(BaseModel):
    """Работа по графику: станок, название, описание, периодичность, плановое время, дата."""
    machine_id: int
    title: str
    description: str = ""
    frequency_days: int = Field(30, ge=1, le=3650)
    planned_hours: float = Field(0, ge=0, le=10000)
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
        raise HTTPException(status_code=422, detail="Название оборудования не может быть пустым")
    mid = execute(
        "INSERT INTO machines (name, model, power, location, notes) VALUES (?, ?, ?, ?, ?)",
        (data.name.strip(), data.model.strip(), data.power.strip(),
         data.location.strip(), data.notes.strip()),
    )
    # Возвращаем созданный объект целиком
    return query("SELECT * FROM machines WHERE id = ?", (mid,), one=True)


@app.put("/api/machines/{mid}")
def update_machine(mid: int, data: MachineIn, user=Depends(require_write)):
    """Изменение станка по id."""
    if not data.name.strip():
        raise HTTPException(status_code=422, detail="Название оборудования не может быть пустым")
    execute(
        "UPDATE machines SET name = ?, model = ?, power = ?, location = ?, notes = ? WHERE id = ?",
        (data.name.strip(), data.model.strip(), data.power.strip(),
         data.location.strip(), data.notes.strip(), mid),
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
        raise HTTPException(status_code=404, detail="Оборудование не найдено")
    tid = execute(
        "INSERT INTO tasks (machine_id, title, description, frequency_days, planned_hours, due_date, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (data.machine_id, data.title.strip(), data.description.strip(),
         data.frequency_days, data.planned_hours, data.due_date, date.today().isoformat()),
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
        "UPDATE tasks SET machine_id = ?, title = ?, description = ?, frequency_days = ?, planned_hours = ?, due_date = ? "
        "WHERE id = ?",
        (data.machine_id, data.title.strip(), data.description.strip(),
         data.frequency_days, data.planned_hours, data.due_date, tid),
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


@app.get("/api/logs")
def all_logs(user=Depends(current_user)):
    """История выполнения по всем работам сразу (для режима «вся история»)."""
    return query(
        "SELECT w.*, t.title AS task_title, m.name AS machine_name "
        "FROM worklogs w JOIN tasks t ON t.id = w.task_id JOIN machines m ON m.id = t.machine_id "
        "ORDER BY w.done_date DESC, w.id DESC"
    )


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
    Сводка, Оборудование, График ППР, История выполнения.

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
    ws.append(["Всего оборудования", len(machines)])
    ws.append(["Всего работ в графике", len(tasks)])
    ws.append(["Просрочено", sum(1 for t in tasks if t["due_date"] < today_iso)])
    ws.append(["Срок на сегодня", sum(1 for t in tasks if t["due_date"] == today_iso)])
    ws.append(["Ближайшие 7 дней", sum(1 for t in tasks if today_iso < t["due_date"] <= soon_iso)])
    ws.append(["Выполнений всего", len(logs)])
    for row in ws.iter_rows(min_row=2):
        row[0].font = Font(bold=True)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 28

    # Лист 2. Оборудование
    ws = wb.create_sheet("Оборудование")
    style_sheet(ws, ["№", "Название", "Модель", "Мощность", "Расположение", "Примечания", "Работ в графике"],
                (6, 32, 16, 14, 20, 34, 16))
    for i, m in enumerate(machines, start=1):
        n = sum(1 for t in tasks if t["machine_id"] == m["id"])
        ws.append([i, m["name"], m["model"], m["power"], m["location"], m["notes"], n])

    # Лист 3. График ППР
    ws = wb.create_sheet("График ППР")
    style_sheet(ws, ["Оборудование", "Модель", "Работа", "Описание", "Периодичность, дн.", "План, ч",
                     "Следующий срок", "Статус", "Последнее выполнение",
                     "Последний результат", "Кто выполнил"],
                (32, 16, 26, 36, 16, 10, 14, 14, 14, 18, 16))
    for t in tasks:
        last = last_done.get(t["id"])
        ws.append([
            t["machine_name"], t["machine_model"], t["title"], t["description"],
            t["frequency_days"], t["planned_hours"] or "",
            t["due_date"], _status_label(t["due_date"]),
            last["done_date"] if last else "",
            f"{last['percent']}%" if last else "",
            last["user"] if last else "",
        ])

    # Лист 4. История выполнения
    ws = wb.create_sheet("История выполнения")
    style_sheet(ws, ["Дата", "Оборудование", "Работа", "Кто", "Результат", "Комментарий"],
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


def _task_occurrences(due_iso: str, freq_days: int, year: int):
    """
    Даты выполнения работы в указанном году по графику:
    от даты ближайшего выполнения шагами по периодичности вперёд/назад.
    Возвращает список объектов date.
    """
    try:
        due = date.fromisoformat(due_iso)
    except (ValueError, TypeError):
        return []
    if not freq_days or freq_days < 1:
        return []
    y0 = date(year, 1, 1)
    y1 = date(year, 12, 31)
    # Ближайшее выполнение, попадающее в год (≥ 1 января)
    if due < y0:
        under = (y0 - due).days
        k = (under + freq_days - 1) // freq_days
        due = due + timedelta(days=k * freq_days)
    else:
        steps = (due - y0).days // freq_days
        due = due - timedelta(days=steps * freq_days)
    occ = []
    cur = due
    while cur <= y1:
        if cur >= y0:
            occ.append(cur)
        cur = cur + timedelta(days=freq_days)
    return occ


@app.get("/api/export_year.xlsx")
def export_year(year: int = 0, user=Depends(current_user)):
    """
    Годовой план ППР. Два листа:

    - «Год»: оборудование (сгруппировано по расположению) x 12 месяцев
      (метка «Х», если в этом месяце есть ремонт) + «Трудоемкость» = сумма
      плановых часов за год;
    - «Год по дням»: оборудование (сгруппировано по расположению) x каждый день
      года; в ячейке — суммарные плановые часы работ этого оборудования на дату.
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
        from itertools import groupby
    except ImportError:
        raise HTTPException(status_code=500, detail="Не установлен openpyxl. Запустите setup.bat")

    if not year:
        year = date.today().year
    y0 = date(year, 1, 1)
    y1 = date(year, 12, 31)
    ndays = (y1 - y0).days + 1

    machines = query("SELECT * FROM machines ORDER BY location, name")
    tasks_all = query("SELECT * FROM tasks")
    by_machine = {}
    for t in tasks_all:
        by_machine.setdefault(t["machine_id"], []).append(t)

    # Расчёт: часы по дням, отмеченные месяцы, годовая трудоёмкость
    day_hours = {}
    month_marks = {}
    year_total = {}
    for m in machines:
        dh = {}
        months = set()
        total = 0.0
        for t in by_machine.get(m["id"], []):
            h = t.get("planned_hours") or 0
            for occ in _task_occurrences(t["due_date"], t["frequency_days"], year):
                dh[occ.isoformat()] = dh.get(occ.isoformat(), 0) + h
                months.add(occ.month)
                total += h
        day_hours[m["id"]] = dh
        month_marks[m["id"]] = months
        year_total[m["id"]] = total

    MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
    ROMAN = ["I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII"]

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="414042")
    loc_font = Font(bold=True, color="2B2B2D")
    loc_fill = PatternFill("solid", fgColor="47C2C7")
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center")
    thin = Side(style="thin", color="C8D0DA")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()

    # ---------------- Лист «Год» ----------------
    ws = wb.active
    ws.title = "Год"
    ws.merge_cells("A1:N1")
    ws["A1"] = f"ГРАФИК ППР оборудования на {year} год"
    ws["A1"].font = Font(bold=True, size=14)
    header = ["Наименование оборудования", "Расположение"] + ROMAN + ["Трудоемкость, чел*час."]
    ws.append([])
    ws.append(header)
    hrow = ws.max_row
    for c in range(1, len(header) + 1):
        cell = ws.cell(row=hrow, column=c)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = center
        cell.border = border
    for m in machines:
        row = [m["name"], m["location"]]
        for mon in range(1, 13):
            row.append("Х" if mon in month_marks[m["id"]] else "")
        total = year_total[m["id"]]
        row.append(round(total, 2) if total else "")
        ws.append(row)
        for c in range(1, len(header) + 1):
            ws.cell(row=ws.max_row, column=c).border = border
    ws.column_dimensions["A"].width = 34
    ws.column_dimensions["B"].width = 18
    for i in range(3, 15):
        ws.column_dimensions[get_column_letter(i)].width = 6
    ws.column_dimensions["N"].width = 18
    ws.freeze_panes = f"C{hrow + 1}"

    # ---------------- Лист «Год по дням» ----------------
    ws2 = wb.create_sheet("Год по дням")
    ws2.cell(row=1, column=1, value="Наименование оборудования")
    cur_m = None
    start = 2
    for i in range(ndays):
        cur = y0 + timedelta(days=i)
        col = 2 + i
        ws2.cell(row=2, column=col, value=cur.day)
        if cur_m is None:
            cur_m = cur.month
            start = col
        elif cur.month != cur_m:
            ws2.merge_cells(start_row=1, start_column=start, end_row=1, end_column=col - 1)
            ws2.cell(row=1, column=start, value=MONTHS[cur_m - 1])
            cur_m = cur.month
            start = col
    ws2.merge_cells(start_row=1, start_column=start, end_row=1, end_column=1 + ndays)
    ws2.cell(row=1, column=start, value=MONTHS[cur_m - 1])
    for col in range(1, ndays + 2):
        c1 = ws2.cell(row=1, column=col)
        c1.alignment = center
        c1.font = head_font
        c1.fill = head_fill
        c2 = ws2.cell(row=2, column=col)
        c2.alignment = center
        c2.font = Font(bold=True, size=9)

    row = 3
    seq = sorted(machines, key=lambda x: (x["location"] or "", x["name"]))
    for loc, group in groupby(seq, key=lambda x: x["location"] or "— без расположения —"):
        ws2.cell(row=row, column=1, value=loc)
        ws2.merge_cells(start_row=row, start_column=1, end_row=row, end_column=1 + ndays)
        lc = ws2.cell(row=row, column=1)
        lc.font = loc_font
        lc.fill = loc_fill
        lc.alignment = left
        row += 1
        for m in group:
            ws2.cell(row=row, column=1, value=m["name"]).alignment = left
            dh = day_hours[m["id"]]
            for i in range(ndays):
                h = dh.get((y0 + timedelta(days=i)).isoformat())
                if h:
                    ws2.cell(row=row, column=2 + i, value=h).alignment = center
            row += 1

    ws2.column_dimensions["A"].width = 32
    for i in range(ndays):
        ws2.column_dimensions[get_column_letter(2 + i)].width = 3.2
    ws2.freeze_panes = "B3"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"Годовой_план_ППР_{year}.xlsx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f"attachment; filename=\"ppr_year_{year}.xlsx\"; "
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