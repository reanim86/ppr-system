"use strict";

const $ = (s) => document.querySelector(s);

const API = "/api";
let TOKEN = localStorage.getItem("ppr_token") || "";
let ME = null;
let MACHINES = [];
let TASKS = [];
let DUE = { overdue: [], today: [], upcoming: [] };
let PREV_DUE_IDS = new Set();

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
function todayIso() {
  const d = new Date();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}
function fmtDate(iso) {
  if (!iso) return "—";
  const [y, m, d] = iso.split("-");
  return `${d}.${m}.${y}`;
}
function isFull() {
  return ME && ME.role !== "viewer";
}

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (TOKEN) opts.headers["Authorization"] = "Bearer " + TOKEN;
  if (body) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(API + path, opts);
  if (res.status === 401) { logout(); throw new Error("Сессия истекла, войдите заново"); }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

/* ---------------- Login / logout ---------------- */

async function doLogin() {
  const username = $("#login-user").value.trim();
  const password = $("#login-pass").value;
  if (!username || !password) { $("#login-error").textContent = "Введите логин и пароль"; return; }
  try {
    const data = await api("/login", "POST", { username, password });
    TOKEN = data.token;
    localStorage.setItem("ppr_token", TOKEN);
    ME = data;
    $("#login-error").textContent = "";
    enterApp();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
}

function logout() {
  TOKEN = "";
  localStorage.removeItem("ppr_token");
  ME = null;
  $("#login-view").classList.remove("hidden");
  $("#app-view").classList.add("hidden");
  $("#login-pass").value = "";
}

async function changePass() {
  const p = prompt("Введите новый пароль (мин. 4 символа):");
  if (!p) return;
  if (p.length < 4) { alert("Пароль слишком короткий"); return; }
  try { await api("/password", "POST", { new_password: p }); alert("Пароль изменён"); }
  catch (e) { alert(e.message); }
}

async function exportExcel() {
  try {
    const res = await fetch(API + "/export.xlsx", {
      headers: { Authorization: "Bearer " + TOKEN },
    });
    if (!res.ok) {
      let detail = `Ошибка ${res.status}`;
      try { const j = await res.json(); if (j.detail) detail = j.detail; } catch (e) {}
      alert(detail);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "Отчет_ППР_" + todayIso() + ".xlsx";
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert("Ошибка экспорта: " + e.message);
  }
}

function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  const roleLabel = ME.role === "viewer" ? "руководитель — просмотр"
    : (ME.role === "admin" ? "руководитель" : "инженер");
  $("#whoami").textContent = `${ME.username} (${roleLabel})`;
  document.querySelectorAll(".tab-btn")[0].click();
  loadAll();
  startPolling();
}

/* ---------------- Tabs ---------------- */

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
    $("#tab-" + btn.dataset.tab).classList.remove("hidden");
  });
});

/* ---------------- Data loading ---------------- */

async function loadAll() {
  try {
    MACHINES = await api("/machines");
    TASKS = await api("/tasks");
    const st = await api("/stats");
    renderMachines();
    renderTasks();
    renderTaskSelects();
    renderCards(st);
    await loadDue();
  } catch (e) {
    if (e.message.includes("Сессия")) return;
    alert("Ошибка загрузки: " + e.message);
  }
}

function renderCards(st) {
  $("#stat-overdue").textContent = st.overdue;
  $("#stat-today").textContent = st.due_today;
  $("#stat-tasks").textContent = st.tasks;
  $("#stat-machines").textContent = st.machines;
}

/* ---------------- Machines ---------------- */

function renderMachines() {
  const tb = $("#machines-table tbody");
  if (!MACHINES.length) {
    tb.innerHTML = `<tr><td colspan="5" class="empty">Нет станков. Добавьте первый станок.</td></tr>`;
    return;
  }
  tb.innerHTML = MACHINES.map((m) => `
    <tr>
      <td>${esc(m.name)}</td>
      <td>${esc(m.model)}</td>
      <td>${esc(m.location)}</td>
      <td>${esc(m.notes)}</td>
      <td class="col-actions">
        ${isFull() ? `
        <button class="ghost" onclick="machineForm(${m.id})">Изменить</button>
        <button class="danger" onclick="deleteMachine(${m.id})">Удалить</button>` : ""}
      </td>
    </tr>`).join("");
}

function machineForm(id = null) {
  const m = id ? MACHINES.find((x) => x.id === id) : null;
  openModal(`
    <h3>${m ? "Изменить станок" : "Добавить станок"}</h3>
    <label>Название *</label>
    <input id="f-name" value="${esc(m?.name)}">
    <label>Модель</label>
    <input id="f-model" value="${esc(m?.model)}">
    <label>Расположение / цех</label>
    <input id="f-loc" value="${esc(m?.location)}">
    <label>Примечания</label>
    <textarea id="f-notes" rows="2">${esc(m?.notes)}</textarea>
    <div class="modal-actions">
      <button class="btn" onclick="saveMachine(${m ? m.id : "null"})">Сохранить</button>
      <button class="ghost" onclick="closeModal()">Отмена</button>
    </div>`);
}

async function saveMachine(id) {
  const body = {
    name: $("#f-name").value.trim(),
    model: $("#f-model").value.trim(),
    location: $("#f-loc").value.trim(),
    notes: $("#f-notes").value.trim(),
  };
  if (!body.name) { alert("Укажите название"); return; }
  try {
    if (id) await api(`/machines/${id}`, "PUT", body);
    else await api("/machines", "POST", body);
    closeModal();
    await loadAll();
  } catch (e) { alert(e.message); }
}

async function deleteMachine(id) {
  if (!confirm("Удалить станок вместе со всеми работами по нему?")) return;
  try { await api(`/machines/${id}`, "DELETE"); await loadAll(); }
  catch (e) { alert(e.message); }
}

/* ---------------- Tasks ---------------- */

function taskStatus(t) {
  const today = todayIso();
  const soon = new Date();
  soon.setDate(soon.getDate() + 7);
  const soonIso = `${soon.getFullYear()}-${String(soon.getMonth() + 1).padStart(2, "0")}-${String(soon.getDate()).padStart(2, "0")}`;
  if (t.due_date < today) return ["status-overdue", "Просрочено"];
  if (t.due_date === today) return ["status-today", "Сегодня"];
  if (t.due_date <= soonIso) return ["status-soon", "Скоро"];
  return ["status-ok", "В графике"];
}

function renderTasks() {
  const filter = $("#tasks-filter").value;
  const list = filter ? TASKS.filter((t) => t.machine_id === parseInt(filter)) : TASKS;
  const tb = $("#tasks-table tbody");
  if (!list.length) {
    tb.innerHTML = `<tr><td colspan="6" class="empty">Нет работ. Добавьте первую.</td></tr>`;
    return;
  }
  tb.innerHTML = list.map((t) => {
    const [cls, label] = taskStatus(t);
    return `
    <tr>
      <td>${esc(t.machine_name)}</td>
      <td><b>${esc(t.title)}</b><br><span class="muted">${esc(t.description)}</span></td>
      <td>${t.frequency_days} дн.</td>
      <td>${fmtDate(t.due_date)}</td>
      <td><span class="status-badge ${cls}">${label}</span></td>
      <td class="col-actions">
        ${isFull() ? `
        <button class="btn green" onclick="completeForm(${t.id})">Выполнено</button>
        <button class="ghost" onclick="taskForm(${t.id})">Изменить</button>
        <button class="danger" onclick="deleteTask(${t.id})">Удалить</button>` : ""}
        <button class="ghost" onclick="openLogs(${t.id})">История</button>
      </td>
    </tr>`;
  }).join("");
}

function taskForm(id = null) {
  const t = id ? TASKS.find((x) => x.id === id) : null;
  const opts = MACHINES.map((m) =>
    `<option value="${m.id}" ${t && t.machine_id === m.id ? "selected" : ""}>${esc(m.name)}</option>`).join("");
  if (!MACHINES.length) { alert("Сначала добавьте станок"); return; }
  openModal(`
    <h3>${t ? "Изменить работу" : "Добавить работу"}</h3>
    <label>Станок *</label>
    <select id="f-mid">${opts}</select>
    <label>Название работы *</label>
    <input id="f-title" value="${esc(t?.title)}">
    <label>Описание</label>
    <textarea id="f-desc" rows="2">${esc(t?.description)}</textarea>
    <label>Периодичность (дней между выполнениями)</label>
    <input id="f-freq" type="number" min="1" max="3650" value="${t?.frequency_days ?? 30}">
    <label>Дата следующего выполнения *</label>
    <input id="f-due" type="date" value="${t?.due_date ?? todayIso()}">
    <div class="modal-actions">
      <button class="btn" onclick="saveTask(${t ? t.id : "null"})">Сохранить</button>
      <button class="ghost" onclick="closeModal()">Отмена</button>
    </div>`);
}

async function saveTask(id) {
  const body = {
    machine_id: parseInt($("#f-mid").value),
    title: $("#f-title").value.trim(),
    description: $("#f-desc").value.trim(),
    frequency_days: parseInt($("#f-freq").value || "30"),
    due_date: $("#f-due").value,
  };
  if (!body.title) { alert("Укажите название работы"); return; }
  if (!body.due_date) { alert("Укажите дату"); return; }
  try {
    if (id) await api(`/tasks/${id}`, "PUT", body);
    else await api("/tasks", "POST", body);
    closeModal();
    await loadAll();
  } catch (e) { alert(e.message); }
}

async function deleteTask(id) {
  if (!confirm("Удалить работу?")) return;
  try { await api(`/tasks/${id}`, "DELETE"); await loadAll(); }
  catch (e) { alert(e.message); }
}

function completeForm(id) {
  const t = TASKS.find((x) => x.id === id);
  if (!t) return;
  openModal(`
    <h3>Отметить выполнение</h3>
    <p><b>${esc(t.machine_name)}</b> — ${esc(t.title)}</p>
    <label>Процент выполнения (0–100)</label>
    <input id="f-percent" type="number" min="0" max="100" value="100">
    <label>Комментарий</label>
    <textarea id="f-comment" rows="2"></textarea>
    <div class="modal-actions">
      <button class="btn green" onclick="saveComplete(${t.id})">Отметить</button>
      <button class="ghost" onclick="closeModal()">Отмена</button>
    </div>`);
}

async function saveComplete(id) {
  const body = {
    percent: parseInt($("#f-percent").value || "100"),
    comment: $("#f-comment").value.trim(),
  };
  if (body.percent < 0 || body.percent > 100) { alert("Процент от 0 до 100"); return; }
  try {
    await api(`/tasks/${id}/complete`, "POST", body);
    closeModal();
    await loadAll();
  } catch (e) { alert(e.message); }
}

/* ---------------- Due / polling ---------------- */

let AUDIO = null;
function initAudio() {
  if (AUDIO) return;
  try { AUDIO = new (window.AudioContext || window.webkitAudioContext)(); }
  catch (e) { AUDIO = null; }
}
document.addEventListener("click", initAudio, { once: true });
document.addEventListener("keydown", initAudio, { once: true });

function beep() {
  if (!AUDIO) return;
  try {
    const t = AUDIO.currentTime;
    for (const [freq, start, dur] of [[880, 0, 0.18], [660, 0.25, 0.18], [880, 0.5, 0.45]]) {
      const osc = AUDIO.createOscillator();
      const gain = AUDIO.createGain();
      osc.type = "sine";
      osc.frequency.value = freq;
      gain.gain.value = 0.25;
      osc.connect(gain);
      gain.connect(AUDIO.destination);
      osc.start(t + start);
      osc.stop(t + start + dur);
    }
  } catch (e) {}
}

function renderTaskRows(tasks) {
  if (!tasks.length) return `<div class="empty">Нет работ.</div>`;
  return `<table><thead><tr>
    <th>Станок</th><th>Работа</th><th>Срок</th><th class="col-actions"></th>
  </tr></thead><tbody>` + tasks.map((t) => `
    <tr>
      <td>${esc(t.machine_name)}</td>
      <td>${esc(t.title)}</td>
      <td>${fmtDate(t.due_date)}</td>
      <td class="col-actions">
        ${isFull() ? `
        <button class="btn green" onclick="completeForm(${t.id})">Выполнено</button>` : ""}
        <button class="ghost" onclick="openLogs(${t.id})">История</button>
      </td>
    </tr>`).join("") + `</tbody></table>`;
}

async function loadDue() {
  try {
    const d = await api("/due");
    DUE = d;
    PREV_DUE_IDS = new Set([...d.overdue, ...d.today].map((t) => t.id));
    renderDue();
    const need = d.overdue.length + d.today.length;
    if (need > 0) {
      beep();
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("Система ППР", {
          body: `Есть работы к выполнению: ${d.overdue.length} просрочено, ${d.today.length} на сегодня.`,
        });
      }
    }
  } catch (e) {}
}

async function pollDue() {
  try {
    const d = await api("/due");
    const nowIds = new Set([...d.overdue, ...d.today].map((t) => t.id));
    const fresh = [...nowIds].filter((id) => !PREV_DUE_IDS.has(id));
    DUE = d;
    PREV_DUE_IDS = nowIds;
    renderDue();
    if (fresh.length) {
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("Система ППР", { body: `Наступил срок по ${fresh.length} работ(ам). Откройте «Сегодня».` });
      }
      beep();
    }
  } catch (e) {}
}

function renderDue() {
  const alertBanner = $("#alert-banner");
  const need = DUE.overdue.length + DUE.today.length;
  if (need > 0) {
    alertBanner.classList.remove("hidden");
    alertBanner.innerHTML = `Внимание: ${DUE.overdue.length} просрочено, ${DUE.today.length} на сегодня. Нажмите, чтобы перейти к списку.`;
    alertBanner.onclick = () => { document.querySelectorAll(".tab-btn")[0].click(); };
  } else {
    alertBanner.classList.add("hidden");
  }

  const dueAll = [...DUE.overdue, ...DUE.today];
  $("#due-list").innerHTML = renderTaskRows(dueAll);
  $("#upcoming-list").innerHTML = renderTaskRows(DUE.upcoming);

  const n = DUE.overdue.length + DUE.today.length;
  document.title = n ? `(${n}) Система ППР` : "Система ППР";
}

function startPolling() {
  if (window._polling) return;
  window._polling = true;
  setInterval(pollDue, 30000);
}

/* ---------------- Logs ---------------- */

function renderTaskSelects() {
  const filter = $("#tasks-filter");
  const cur = filter.value;
  filter.innerHTML = `<option value="">Все станки</option>` +
    MACHINES.map((m) => `<option value="${m.id}">${esc(m.name)}</option>`).join("");
  filter.value = cur;

  const sel = $("#logs-task-select");
  const curLog = sel.value;
  sel.innerHTML = `<option value="">— выберите работу —</option>` +
    TASKS.map((t) => `<option value="${t.id}">${esc(t.machine_name)} — ${esc(t.title)}</option>`).join("");
  sel.value = curLog;
}

async function openLogs(id) {
  document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
  document.querySelector('[data-tab="logs"]').classList.add("active");
  document.querySelectorAll(".tab").forEach((t) => t.classList.add("hidden"));
  $("#tab-logs").classList.remove("hidden");
  $("#logs-task-select").value = String(id);
  renderLogs();
}

async function renderLogs() {
  const id = $("#logs-task-select").value;
  const tb = $("#logs-table tbody");
  if (!id) { tb.innerHTML = `<tr><td colspan="4" class="empty">Выберите работу</td></tr>`; return; }
  try {
    const logs = await api(`/tasks/${id}/logs`);
    if (!logs.length) { tb.innerHTML = `<tr><td colspan="4" class="empty">Выполнений пока нет</td></tr>`; return; }
    tb.innerHTML = logs.map((l) => `
      <tr>
        <td>${fmtDate(l.done_date)}</td>
        <td>${esc(l.user)}</td>
        <td><span class="status-badge ${l.percent >= 100 ? "status-ok" : "status-today"}">${l.percent}%</span></td>
        <td>${esc(l.comment)}</td>
      </tr>`).join("");
  } catch (e) { tb.innerHTML = `<tr><td colspan="4" class="empty">Ошибка: ${esc(e.message)}</td></tr>`; }
}

/* ---------------- Modal ---------------- */

function openModal(html) {
  $("#modal-body").innerHTML = html;
  $("#modal-overlay").classList.remove("hidden");
}

function closeModal() {
  $("#modal-overlay").classList.add("hidden");
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

/* ---------------- Init ---------------- */

(async function init() {
  if (TOKEN) {
    try {
      ME = await api("/me");
      enterApp();
      return;
    } catch (e) {}
  }
  $("#login-user").focus();
})();