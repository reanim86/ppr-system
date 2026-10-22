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
        from openpyxl.worksheet.properties import PageSetupProperties
        from openpyxl.chart import BarChart, Reference
        from openpyxl.chart.data_source import AxDataSource, StrRef
        from openpyxl.chart.shapes import GraphicalProperties
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
    loc_fill = PatternFill("solid", fgColor="E6F4F5")
    center = Alignment(horizontal="center", vertical="center")
    left = Alignment(horizontal="left", vertical="center")
    wrap_left = Alignment(horizontal="left", vertical="top", wrap_text=True)
    thin = Side(style="thin", color="808080")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    wb = Workbook()

    # ---------------- Лист «Год» ----------------
    ws = wb.active
    ws.title = "Год"
    TOTAL_COLS = 15            # A + B + 12 месяцев + Трудоемкость
    MONTH_COL0 = 3             # первый столбец месяцев (C)
    MONTH_COL_LAST = 14        # последний столбец месяцев (N)
    TOTAL_COL = 15             # столбец «Трудоемкость» (O)

    # Шапка (строки 2-9) — как в примере
    ws["I2"] = "УТВЕРЖДАЮ:"
    ws["I3"] = "Директор"
    ws["I4"] = 'ООО "СиТри Индастри"'
    ws["I5"] = "______________"
    ws["L5"] = "А.С. Плешков"
    ws["I6"] = f'"_____" ________________ {year} г.'
    for r, txt, sz in ((7, "ГРАФИК ППР", 14),
                       (8, 'оборудования ООО "СиТри Индастри"', 12),
                       (9, f"на {year} г.", 12)):
        c = ws.cell(row=r, column=2, value=txt)
        c.font = Font(bold=True, size=sz)
        c.alignment = Alignment(horizontal="left", vertical="center")

    # Заголовки таблицы: строка 11 — названия, 12 — месяцы, 13 — номера столбцов
    hr = 11
    ws.cell(row=hr, column=1, value="Наименование оборудования")
    ws.cell(row=hr, column=2, value="Ед.изм.")
    ws.merge_cells(start_row=hr, start_column=MONTH_COL0, end_row=hr, end_column=MONTH_COL_LAST)
    ws.cell(row=hr, column=MONTH_COL0, value="Месяц проведения ремонта")
    ws.merge_cells(start_row=hr, start_column=TOTAL_COL, end_row=hr + 1, end_column=TOTAL_COL)
    ws.cell(row=hr, column=TOTAL_COL, value="Трудоемкость, чел*час.")
    for i, rm in enumerate(ROMAN):
        ws.cell(row=hr + 1, column=MONTH_COL0 + i, value=rm)
    for cnum in range(1, TOTAL_COLS + 1):
        ws.cell(row=hr + 2, column=cnum, value=cnum)
    for rr in (hr, hr + 1, hr + 2):
        for cc in range(1, TOTAL_COLS + 1):
            cell = ws.cell(row=rr, column=cc)
            cell.font = Font(bold=True)
            cell.alignment = center
            cell.border = border
    ws.cell(row=hr, column=1).alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
    ws.cell(row=hr, column=2).alignment = left
    ws.cell(row=hr, column=TOTAL_COL).alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    # Данные (сгруппированы по расположению)
    row = hr + 3
    seq = sorted(machines, key=lambda x: (x["location"] or "", x["name"]))
    for loc, group in groupby(seq, key=lambda x: x["location"] or "— без расположения —"):
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=TOTAL_COLS)
        gc = ws.cell(row=row, column=1, value=loc)
        gc.font = loc_font
        gc.alignment = left
        for cc in range(1, TOTAL_COLS + 1):
            ws.cell(row=row, column=cc).border = border
        row += 1
        for m in group:
            ws.cell(row=row, column=1, value=m["name"]).alignment = wrap_left
            ws.cell(row=row, column=2, value=m.get("power") or "")
            for mon in range(1, 13):
                ws.cell(row=row, column=MONTH_COL0 - 1 + mon,
                        value="Х" if mon in month_marks[m["id"]] else "")
            total = year_total[m["id"]]
            ws.cell(row=row, column=TOTAL_COL, value=round(total, 2) if total else "")
            for cc in range(1, TOTAL_COLS + 1):
                ws.cell(row=row, column=cc).border = border
            row += 1

    # Внешняя рамка основной таблицы
    last_row = row - 1
    outer = Side(style="medium", color="424242")
    for rr in range(hr, last_row + 1):
        for cc in range(1, TOTAL_COLS + 1):
            cell = ws.cell(row=rr, column=cc)
            b = cell.border
            try:
                cell.border = Border(
                    left=outer if cc == 1 else b.left,
                    right=outer if cc == TOTAL_COLS else b.right,
                    top=outer if rr == hr else b.top,
                    bottom=outer if rr == last_row else b.bottom,
                )
            except Exception:
                pass

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 9
    for i in range(MONTH_COL0, MONTH_COL_LAST + 1):
        ws.column_dimensions[get_column_letter(i)].width = 4.3
    ws.column_dimensions[get_column_letter(TOTAL_COL)].width = 13
    ws.freeze_panes = f"C{hr + 3}"

    # Печать: книжная ориентация, вписать в одну страницу по ширине, поля в см (→ дюймы)
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.print_area = f"A1:{get_column_letter(TOTAL_COLS)}{last_row}"
    ws.page_margins.top = 1.9 / 2.54
    ws.page_margins.bottom = 1.9 / 2.54
    ws.page_margins.left = 0.6 / 2.54
    ws.page_margins.right = 0.6 / 2.54
    ws.page_margins.header = 0.8 / 2.54
    ws.page_margins.footer = 0.8 / 2.54

    # ---------------- Лист «Год по дням» ----------------
    ws2 = wb.create_sheet("Год по дням")
    gap_fill = PatternFill("solid", fgColor="414042")
    day_col = {}      # iso-дата -> индекс столбца
    gap_cols = []     # индексы разделительных столбцов между месяцами

    # Разбивка дней по месяцам
    month_days = []
    for i in range(ndays):
        cur = y0 + timedelta(days=i)
        if not month_days or month_days[-1][0] != cur.month:
            month_days.append((cur.month, []))
        month_days[-1][1].append(cur)

    ws2.cell(row=1, column=1, value="Наименование оборудования")
    col = 2
    for mi, (mon, days) in enumerate(month_days):
        if mi > 0:                      # отступ между месяцами
            gap_cols.append(col)
            col += 1
        start = col
        for d in days:
            day_col[d.isoformat()] = col
            ws2.cell(row=2, column=col, value=d.day)
            col += 1
        ws2.merge_cells(start_row=1, start_column=start, end_row=1, end_column=col - 1)
        ws2.cell(row=1, column=start, value=MONTHS[mon - 1])
    last_col = col - 1

    for c in range(1, last_col + 1):
        c1 = ws2.cell(row=1, column=c)
        c1.alignment = center
        c1.font = head_font
        c1.fill = head_fill
        c2 = ws2.cell(row=2, column=c)
        c2.alignment = center
        c2.font = Font(bold=True, size=9)

    # Данные, сгруппированные по расположению; подсчёт суммы по дням
    col_totals = {}
    row = 3
    seq = sorted(machines, key=lambda x: (x["location"] or "", x["name"]))
    for loc, group in groupby(seq, key=lambda x: x["location"] or "— без расположения —"):
        for c in range(1, last_col + 1):
            ws2.cell(row=row, column=c).fill = loc_fill
        lc = ws2.cell(row=row, column=1, value=loc)
        lc.font = loc_font
        lc.alignment = left
        row += 1
        for m in group:
            ws2.cell(row=row, column=1, value=m["name"]).alignment = left
            for iso, h in day_hours[m["id"]].items():
                if h:
                    ws2.cell(row=row, column=day_col[iso], value=h).alignment = center
                    col_totals[iso] = col_totals.get(iso, 0) + h
            row += 1

    # Итоговая строка: суммарные часы по каждому дню
    total_row = row
    for c in range(1, last_col + 1):
        try:
            ws2.cell(row=total_row, column=c).fill = loc_fill
        except Exception:
            pass
    tc = ws2.cell(row=total_row, column=1, value="Итого, ч")
    tc.font = Font(bold=True)
    tc.alignment = left
    for iso, total in col_totals.items():
        cell = ws2.cell(row=total_row, column=day_col[iso], value=round(total, 2))
        cell.font = Font(bold=True)
        cell.alignment = center

    # Заливка разделительных столбцов (отступ между месяцами)
    for gc in gap_cols:
        for rr in range(1, total_row + 1):
            try:
                ws2.cell(row=rr, column=gc).fill = gap_fill
            except Exception:
                pass

    ws2.column_dimensions["A"].width = 32
    for iso, cc in day_col.items():
        ws2.column_dimensions[get_column_letter(cc)].width = 3.2
    for gc in gap_cols:
        ws2.column_dimensions[get_column_letter(gc)].width = 2.4
    ws2.freeze_panes = "B3"

    # ---------------- Лист «Диаграмма Ганта» ----------------
    # Оборудование — по оси Y (одна строка на станок), по оси X — дни года.
    # Каждая работа с плановым временем (> 0) — полоса в день её выполнения
    # (длина полосы = длительность в днях, часы/24).
    # Зум по месяцу выполняется вручную в Excel: «Формат оси → Границы».
    equip_events = []
    maxk = 0
    for m in seq:
        evs = []
        for t in by_machine.get(m["id"], []):
            h = t.get("planned_hours") or 0
            if h <= 0:
                continue
            for occ in _task_occurrences(t["due_date"], t["frequency_days"], year):
                evs.append((occ.timetuple().tm_yday, float(h) / 24.0))
        evs.sort()
        equip_events.append((m["name"], evs))
        maxk = max(maxk, len(evs))

    gdata = wb.create_sheet("Данные Ганта")
    header = ["Оборудование"]
    for k in range(1, maxk + 1):
        header += [f"s{k}", f"d{k}"]
    gdata.append(header)
    for name, evs in equip_events:
        rowvals = [name]
        prev_end = 0.0
        for k in range(maxk):
            if k < len(evs):
                pos, dur = evs[k]
                rowvals += [max(0.0, pos - prev_end), dur]
                prev_end = pos + dur
            else:
                rowvals += [0, 0]
        gdata.append(rowvals)
    gdata.sheet_state = "hidden"

    gw = wb.create_sheet("Диаграмма Ганта")
    gw["B2"] = f"Диаграмма Ганта — план ремонтов на {year} г. (по оси X — дни года; для зума: Формат оси → Границы)"
    gw["B2"].font = Font(bold=True, size=12)
    if maxk > 0 and equip_events:
        chart = BarChart()
        chart.type = "bar"           # горизонтальные полосы (Гант)
        chart.grouping = "stacked"
        chart.overlap = 100
        chart.title = f"План ППР на {year} г."
        last_col = 1 + 2 * maxk
        data = Reference(gdata, min_col=2, max_col=last_col, min_row=1, max_row=1 + len(equip_events))
        chart.add_data(data, titles_from_data=True)
        # категории — строковые имена оборудования (иначе Excel не показывает названия)
        cat_ref = f"'Данные Ганта'!$A$2:$A${1 + len(equip_events)}"
        for s in chart.series:
            s.cat = AxDataSource(strRef=StrRef(f=cat_ref))
        # нечётные ряды (смещение) — прозрачные, чётные (длительность) — фирменный цвет
        for si in range(len(chart.series)):
            if si % 2 == 0:
                chart.series[si].graphicalProperties = GraphicalProperties(noFill=True)
            else:
                chart.series[si].graphicalProperties = GraphicalProperties(solidFill="47C2C7")
        chart.legend = None          # убрать расшифровку рядов (s1/d1/...)
        chart.x_axis.title = "Оборудование"
        chart.y_axis.title = "День года"
        chart.x_axis.axPos = "l"
        chart.y_axis.axPos = "b"
        chart.x_axis.delete = False
        chart.y_axis.delete = False
        chart.y_axis.scaling.min = 1
        chart.y_axis.scaling.max = ndays
        chart.y_axis.majorUnit = 7
        chart.height = max(9, len(equip_events) * 0.55)
        chart.width = 30
        gw.add_chart(chart, "B4")
    else:
        gw["B4"] = "Нет работ с заданным плановым временем за выбранный год."

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