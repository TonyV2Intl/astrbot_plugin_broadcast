/**
 * AstrBot 广播插件 · 可视化配置台
 *
 * 与后端通过 AstrBot 注入的 bridge 通信：
 *   window.AstrBotPluginView.apiGet(endpoint, params)
 *   window.AstrBotPluginView.apiPost(endpoint, body)
 *   window.AstrBotPluginPage 是其向后兼容别名。
 * endpoint 不需要带插件名，Dashboard 会自动补全
 * /api/v1/plugins/extensions/<plugin_name>/ 前缀。
 */

const PLUGIN_ENDPOINT = {
  get: "config/get",
  save: "config/save",
  refresh: "config/refresh",
};

const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
const MAX_SCHEDULES = 20;
const MAX_RENDER_ROWS = 400;

const state = {
  bridge: null,
  settings: null,
  sessions: [],
  platforms: [],
  messageTypes: [],
  overview: {},
  filters: { q: "", platform: "", type: "", status: "all" },
  schedules: [],
  runtime: {},
  saving: false,
};

/* ------------------------------------------------------------------ */
/* Bridge                                                              */
/* ------------------------------------------------------------------ */
async function waitForBridge() {
  for (let i = 0; i < 80; i += 1) {
    const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
    if (bridge) return bridge;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(
    "没有找到 AstrBot 插件视图桥接（AstrBotPluginView）。请确认 AstrBot ≥ 4.24.2，并重载插件。"
  );
}

const api = {
  get(endpoint, params) {
    return state.bridge.apiGet(endpoint, params);
  },
  post(endpoint, body) {
    return state.bridge.apiPost(endpoint, body);
  },
};

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */
const $ = (id) => document.getElementById(id);

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", Boolean(isError));
  el.classList.add("show");
  window.clearTimeout(toast._timer);
  toast._timer = window.setTimeout(() => el.classList.remove("show"), 3200);
}

function splitList(value) {
  return String(value || "")
    .split(/[\s,，;；]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatDateTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}月${date.getDate()}日 ${pad(date.getHours())}:${pad(
    date.getMinutes()
  )}`;
}

function messageTypeLabel(type) {
  if (type === "GroupMessage") return "群聊";
  if (type === "FriendMessage") return "私聊";
  if (type === "PrivateMessage") return "私聊";
  return type || "未知";
}

function describeTask(task) {
  const time = task.time || "09:00";
  if (task.mode === "weekly") {
    const days = [...(task.weekdays || [])].sort((a, b) => a - b);
    if (!days.length) return `每周 ${time}（未选星期）`;
    return `每周 ${days.map((d) => WEEKDAY_LABELS[d]).join("、")} ${time}`;
  }
  if (task.mode === "once") {
    return `${task.date || "未选日期"} ${time} 仅一次`;
  }
  return `每天 ${time}`;
}

function pad(n) {
  return String(n).padStart(2, "0");
}

function dateStr(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 默认时间：下一个整点，避免新建任务时填一个已经过去的时间。 */
function defaultTime() {
  const now = new Date();
  now.setMinutes(0, 0, 0);
  now.setHours(now.getHours() + 1);
  if (now.getDate() !== new Date().getDate()) return "09:00";
  return `${pad(now.getHours())}:00`;
}

/** 仅一次任务默认日期：今天还没到时间就今天，否则明天（和设闹钟一致）。 */
function defaultDate(time) {
  const now = new Date();
  const [hour, minute] = String(time || "09:00").split(":").map(Number);
  const target = new Date(now);
  target.setHours(hour, minute, 0, 0);
  if (target.getTime() <= now.getTime()) {
    target.setDate(target.getDate() + 1);
  }
  return dateStr(target);
}

function newId() {
  const bytes = new Uint8Array(3);
  crypto.getRandomValues(bytes);
  return "t" + [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function makeIcon(kind) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "15");
  svg.setAttribute("height", "15");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const ICONS = {
    trash: [
      "M4 7h16",
      "M6.5 7l.9 11.2A2 2 0 0 0 9.4 20h5.2a2 2 0 0 0 2-1.8L17.5 7",
      "M9.5 7V5.5A1.5 1.5 0 0 1 11 4h2a1.5 1.5 0 0 1 1.5 1.5V7",
      "M10.5 11v5",
      "M13.5 11v5",
    ],
  };
  const paths = ICONS[kind] || ICONS.trash;
  paths.forEach((d) => {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.appendChild(path);
  });
  return svg;
}

function targetSet() {
  return new Set(state.settings?.target_umos || []);
}

/* ------------------------------------------------------------------ */
/* 渲染                                                                */
/* ------------------------------------------------------------------ */
function renderOverview() {
  const overview = state.overview || {};
  const settings = state.settings || {};
  const sessions = state.sessions || [];
  const targets = targetSet();

  $("stat-total").textContent = String(sessions.length);
  $("stat-target").innerHTML = `${targets.size}` + `<small> / ${sessions.length}</small>`;
  $("stat-quota").innerHTML =
    settings.daily_limit > 0
      ? `${overview.today_sent ?? 0}<small> / ${settings.daily_limit}</small>`
      : `${overview.today_sent ?? 0}<small> / 不限</small>`;

  const activeTasks = (state.schedules || []).filter((task) => task.enabled).length;
  $("stat-next").textContent = overview.next_run_at
    ? formatDateTime(overview.next_run_at)
    : activeTasks
      ? "待保存"
      : "未开启";

  const banner = $("runtime-banner");
  const problems = [];
  if (overview.schedule_error) problems.push(overview.schedule_error);
  if (overview.scheduler_available === false) {
    problems.push("未安装 apscheduler，定时广播不可用");
  }
  if (overview.busy) problems.push("当前有广播任务正在执行");

  if (problems.length) {
    banner.className = "banner warn";
    banner.textContent = problems.join("；");
  } else if (activeTasks) {
    banner.className = "banner ok";
    banner.textContent =
      `定时广播已生效：${activeTasks} 个任务` +
      (overview.next_run_at ? `，最近一次 ${formatDateTime(overview.next_run_at)}` : "");
  } else {
    banner.className = "banner hidden";
    banner.textContent = "";
  }
}

function renderModeBanner() {
  const banner = $("mode-banner");
  const targets = targetSet();
  const total = state.sessions.length;

  if (targets.size === 0) {
    banner.className = "banner warn";
    banner.innerHTML =
      `当前没有勾选任何会话，<strong>广播不会发送</strong>。` +
      `请在下方勾选目标（共 ${total} 个已知会话）。`;
  } else {
    banner.className = "banner ok";
    banner.innerHTML = `将向 <strong>${targets.size}</strong> 个已勾选的会话广播。新发现的会话会自动勾选。`;
  }
}

function todayWeekday() {
  return (new Date().getDay() + 6) % 7;
}

function field(labelText) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  wrap.appendChild(label);
  return wrap;
}

function buildTaskCard(task) {
  const card = document.createElement("div");
  card.className = "task";
  card.dataset.id = task.id;

  /* 标题行 */
  const head = document.createElement("div");
  head.className = "task-head";

  const title = document.createElement("div");
  title.className = "task-title";
  const dot = document.createElement("span");
  dot.className = "dot";
  const titleText = document.createElement("span");
  title.append(dot, titleText);

  const actions = document.createElement("div");
  actions.className = "task-actions";

  const toggleLabel = document.createElement("label");
  toggleLabel.className = "switch";
  toggleLabel.title = "启用或停用该任务";
  const toggle = document.createElement("input");
  toggle.type = "checkbox";
  toggle.checked = task.enabled !== false;
  const toggleText = document.createElement("span");
  toggleLabel.append(toggle, toggleText);

  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "icon-btn";
  delBtn.title = "删除该任务";
  delBtn.appendChild(makeIcon("trash"));

  actions.append(toggleLabel, delBtn);
  head.append(title, actions);

  const meta = document.createElement("div");
  meta.className = "task-meta";

  /* 表单 */
  const grid = document.createElement("div");
  grid.className = "grid";

  const modeField = field("重复方式");
  const seg = document.createElement("div");
  seg.className = "seg";
  const modeButtons = [
    ["daily", "每天"],
    ["weekly", "每周"],
    ["once", "仅一次"],
  ].map(([mode, label]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.mode = mode;
    btn.textContent = label;
    seg.appendChild(btn);
    return btn;
  });
  modeField.appendChild(seg);

  const timeField = field("发送时间");
  const timeInput = document.createElement("input");
  timeInput.type = "time";
  timeInput.step = "60";
  timeInput.value = task.time || "09:00";
  timeField.appendChild(timeInput);

  const dateField = field("执行日期");
  const dateInput = document.createElement("input");
  dateInput.type = "date";
  dateInput.value = task.date || "";
  const dateHint = document.createElement("span");
  dateHint.className = "hint";
  dateHint.textContent = "到点执行一次后自动停用，像闹钟一样。";
  dateField.append(dateInput, dateHint);

  grid.append(modeField, timeField, dateField);

  const weekField = field("选择星期");
  const weekBox = document.createElement("div");
  weekBox.className = "weekdays";
  const weekButtons = WEEKDAY_LABELS.map((label, index) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.dataset.day = String(index);
    btn.textContent = label;
    weekBox.appendChild(btn);
    return btn;
  });
  weekField.appendChild(weekBox);

  const msgField = field("消息内容");
  const textarea = document.createElement("textarea");
  textarea.maxLength = 4000;
  textarea.placeholder = "例如：早安，今天的例行维护安排在 20:00，请提前保存工作。";
  textarea.value = task.message || "";
  msgField.appendChild(textarea);

  card.append(head, meta, grid, weekField, msgField);

  /* 渲染同步 */
  function refreshMeta() {
    const runtime = state.runtime[task.id] || {};
    let text = "";
    let warn = false;
    if (runtime.error) {
      text = runtime.error;
      warn = true;
    } else if (runtime.pending) {
      text = "已到点，排队等待当前广播结束后立即发送";
    } else if (task.enabled === false) {
      text = task.last_fired
        ? `已于 ${formatDateTime(task.last_fired)} 执行完成`
        : "已停用，保存后不会触发";
    } else if (runtime.next_run_at) {
      text = `下次触发 ${formatDateTime(runtime.next_run_at)}`;
    } else {
      text = "保存后生效";
    }
    meta.textContent = text;
    meta.classList.toggle("warn", warn);
  }

  function refresh() {
    titleText.textContent = describeTask(task);
    card.classList.toggle("off", task.enabled === false);
    toggle.checked = task.enabled !== false;
    toggleText.textContent = task.enabled === false ? "停用" : "启用";
    dateField.classList.toggle("hidden", task.mode !== "once");
    weekField.classList.toggle("hidden", task.mode !== "weekly");
    modeButtons.forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === task.mode);
    });
    weekButtons.forEach((btn) => {
      btn.classList.toggle("active", (task.weekdays || []).includes(Number(btn.dataset.day)));
    });
    if (timeInput.value !== task.time) timeInput.value = task.time;
    if (dateInput.value !== (task.date || "")) dateInput.value = task.date || "";
    refreshMeta();
    updateSaveSummary();
  }

  modeButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      task.mode = btn.dataset.mode;
      if (task.mode === "weekly" && !(task.weekdays || []).length) {
        task.weekdays = [todayWeekday()];
      }
      if (task.mode === "once" && !task.date) {
        task.date = defaultDate(task.time);
      }
      refresh();
    });
  });

  weekButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const day = Number(btn.dataset.day);
      const days = new Set(task.weekdays || []);
      if (days.has(day)) days.delete(day);
      else days.add(day);
      task.weekdays = [...days].sort((a, b) => a - b);
      refresh();
    });
  });

  timeInput.addEventListener("change", () => {
    task.time = timeInput.value || "09:00";
    if (task.mode === "once" && !task.date) task.date = defaultDate(task.time);
    refresh();
  });

  dateInput.addEventListener("change", () => {
    task.date = dateInput.value || "";
    refresh();
  });

  textarea.addEventListener("input", () => {
    task.message = textarea.value;
    updateSaveSummary();
  });

  toggle.addEventListener("change", () => {
    task.enabled = toggle.checked;
    refresh();
  });

  delBtn.addEventListener("click", () => {
    const index = state.schedules.indexOf(task);
    if (index >= 0) state.schedules.splice(index, 1);
    renderSchedules();
    toast("已删除该定时任务，保存后生效");
  });

  refresh();
  return card;
}

/** 一次性任务跑完会自动停用并记下执行时间，这类任务可以一键清理。 */
function isFinished(task) {
  return task.mode === "once" && task.enabled === false && Boolean(task.last_fired);
}

function renderSchedules() {
  const list = $("schedule-list");
  list.innerHTML = "";
  state.schedules.forEach((task) => list.appendChild(buildTaskCard(task)));
  $("schedule-empty").classList.toggle("hidden", state.schedules.length > 0);
  $("add-schedule").disabled = state.schedules.length >= MAX_SCHEDULES;

  const finished = state.schedules.filter(isFinished).length;
  const clearBtn = $("clear-finished");
  clearBtn.disabled = finished === 0;
  clearBtn.textContent = finished ? `清理已执行 (${finished})` : "清理已执行";
  clearBtn.title = finished
    ? `移除 ${finished} 个已执行完成的一次性任务`
    : "没有已执行完成的一次性任务";

  updateSaveSummary();
}

function clearFinished() {
  const before = state.schedules.length;
  state.schedules = state.schedules.filter((task) => !isFinished(task));
  const removed = before - state.schedules.length;
  if (!removed) {
    toast("没有已执行完成的任务可清理", true);
    return;
  }
  renderSchedules();
  toast(`已移除 ${removed} 个已执行任务，保存后生效`);
}

function addSchedule() {
  if (state.schedules.length >= MAX_SCHEDULES) {
    toast(`最多只能添加 ${MAX_SCHEDULES} 个定时任务`, true);
    return;
  }
  const time = defaultTime();
  const task = {
    id: newId(),
    message: "",
    enabled: true,
    mode: "daily",
    time,
    weekdays: [todayWeekday()],
    date: "",
    last_fired: "",
  };
  state.schedules.push(task);
  renderSchedules();
  const textarea = document.querySelector(`.task[data-id="${task.id}"] textarea`);
  if (textarea) {
    textarea.focus();
    textarea.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function fillSelect(select, values, current, labeler, placeholder) {
  select.innerHTML = "";
  const empty = document.createElement("option");
  empty.value = "";
  empty.textContent = placeholder || "全部";
  select.appendChild(empty);
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = labeler ? labeler(value) : value;
    select.appendChild(option);
  });
  if (current && !values.includes(current)) {
    const option = document.createElement("option");
    option.value = current;
    option.textContent = labeler ? labeler(current) : current;
    select.appendChild(option);
  }
  select.value = current || "";
}

function renderFilters() {
  const platformLabel = (pid) => {
    const hit = state.sessions.find((session) => session.platform === pid);
    const adapter = hit?.adapter || "";
    return adapter && adapter !== pid ? `${adapter} · ${pid}` : pid || "未知平台";
  };
  fillSelect(
    $("filter-platform"),
    state.platforms,
    state.filters.platform,
    platformLabel,
    "全部平台"
  );
  fillSelect(
    $("filter-type"),
    state.messageTypes,
    state.filters.type,
    messageTypeLabel,
    "全部场景"
  );
}

function filteredSessions() {
  const { q, platform, type, status } = state.filters;
  const keyword = q.trim().toLowerCase();
  const targets = targetSet();

  return state.sessions.filter((session) => {
    if (platform && session.platform !== platform) return false;
    if (type && session.message_type !== type) return false;

    const selected = targets.has(session.umo);
    if (status === "selected" && !selected) return false;
    if (status === "unselected" && selected) return false;

    if (!keyword) return true;
    return (
      session.umo.toLowerCase().includes(keyword) ||
      String(session.display_name || "").toLowerCase().includes(keyword) ||
      String(session.session_id || "").toLowerCase().includes(keyword)
    );
  });
}

function renderSessions() {
  const list = $("session-list");
  const view = filteredSessions();
  const targets = targetSet();
  list.innerHTML = "";

  if (!view.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.sessions.length
      ? "没有符合筛选条件的会话。"
      : "尚未发现任何会话。让机器人在目标群或私聊里发一条消息，或挂载 aiocqhttp 后点击「刷新会话列表」。";
    list.appendChild(empty);
  } else {
    const fragment = document.createDocumentFragment();
    view.slice(0, MAX_RENDER_ROWS).forEach((session) => {
      const isSelected = targets.has(session.umo);

      const row = document.createElement("div");
      row.className = "session-row";
      row.dataset.umo = session.umo;

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = isSelected;
      checkbox.addEventListener("change", () => {
        toggleInclude(session.umo, checkbox.checked);
      });

      const main = document.createElement("div");
      main.className = "main";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = session.display_name || session.session_id;
      const umo = document.createElement("span");
      umo.className = "umo";
      umo.textContent = session.umo;
      main.append(name, umo);

      const platformBadge = document.createElement("span");
      platformBadge.className = "badge platform";
      platformBadge.textContent = session.adapter || session.platform;
      platformBadge.title = `平台实例：${session.platform}`;

      const typeBadge = document.createElement("span");
      typeBadge.className =
        "badge " + (session.message_type === "GroupMessage" ? "group" : "private");
      typeBadge.textContent = messageTypeLabel(session.message_type);

      row.append(checkbox, main, platformBadge, typeBadge);
      fragment.appendChild(row);
    });
    list.appendChild(fragment);
  }

  const shown = Math.min(view.length, MAX_RENDER_ROWS);
  $("list-count").textContent =
    `显示 ${shown} / 筛选结果 ${view.length} / 全部 ${state.sessions.length}` +
    (view.length > MAX_RENDER_ROWS ? "（请用搜索缩小范围）" : "");

  renderModeBanner();
  renderOverview();
  updateSaveSummary();
}

function renderLastResult() {
  const box = $("last-result");
  const result = state.overview?.last_result;
  if (!result) {
    box.className = "banner";
    box.textContent = "暂无广播记录。";
    return;
  }
  const source = result.source === "schedule" ? "定时" : "手动";
  box.className = "banner";
  box.textContent =
    `上次广播（${source}）${result.time || ""}：目标 ${result.total}，` +
    `成功 ${result.success}，失败 ${result.failed}` +
    (result.skipped ? `，跳过 ${result.skipped}` : "");
}

function renderAll() {
  const settings = state.settings || {};
  $("admin-uids").value = (settings.admin_uids || []).join(", ");
  $("send-interval").value = settings.send_interval ?? 2;
  $("max-per-minute").value = settings.max_per_minute ?? 20;
  $("daily-limit").value = settings.daily_limit ?? 1000;
  $("timezone").value = settings.timezone || "Asia/Shanghai";

  renderFilters();
  renderSchedules();
  renderSessions();
  renderLastResult();
}

function updateSaveSummary() {
  const settings = state.settings || {};
  const targets = targetSet();
  const mode = targets.size ? `已选 ${targets.size} 个会话` : "未选会话（不会广播）";
  const active = (state.schedules || []).filter((task) => task.enabled).length;
  const schedule = active ? `${active} 个定时任务` : "定时未开启";
  $("save-summary").textContent = `${mode}　·　${schedule}　·　间隔 ${settings.send_interval}s`;
}

/* ------------------------------------------------------------------ */
/* 交互                                                                */
/* ------------------------------------------------------------------ */
function toggleInclude(umo, checked) {
  const set = targetSet();
  if (checked) {
    set.add(umo);
  } else {
    set.delete(umo);
  }
  applyTargets(set);
}

function applyTargets(set) {
  const ordered = [];
  state.sessions.forEach((session) => {
    if (set.has(session.umo)) ordered.push(session.umo);
  });
  set.forEach((umo) => {
    if (!ordered.includes(umo)) ordered.push(umo);
  });
  state.settings.target_umos = ordered;
  if (ordered.length === 0) {
    toast("已取消全部勾选：广播不会发送");
  }
  renderSessions();
}

function collectForm() {
  const settings = state.settings || {};
  return {
    admin_uids: splitList($("admin-uids").value),
    target_umos: settings.target_umos || [],
    seen_umos: settings.seen_umos || [],
    send_interval: Number($("send-interval").value || 0),
    max_per_minute: Number($("max-per-minute").value || 0),
    daily_limit: Number($("daily-limit").value || 0),
    schedules: state.schedules.map((task) => ({
      id: task.id,
      message: task.message || "",
      enabled: task.enabled !== false,
      mode: task.mode,
      time: task.time || "09:00",
      weekdays: [...(task.weekdays || [])].sort((a, b) => a - b),
      date: task.mode === "once" ? task.date || "" : "",
      last_fired: task.last_fired || "",
    })),
    timezone: $("timezone").value.trim() || "Asia/Shanghai",
  };
}

async function save() {
  if (state.saving) return;
  const empty = state.schedules.filter(
    (task) => task.enabled !== false && !String(task.message || "").trim()
  ).length;
  state.saving = true;
  const button = $("save");
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const payload = collectForm();
    const data = await api.post(PLUGIN_ENDPOINT.save, payload);
    applyPayload(data);
    let note = "";
    if (!payload.schedules.length) note = "已保存";
    else if (data.schedule_ok) note = `已保存，${data.schedule_note}`;
    else note = `已保存：${data.schedule_note}`;
    if (empty) note += `（${empty} 个任务消息为空，到点不会发送）`;
    toast(note);
  } catch (error) {
    toast(`保存失败：${error.message || error}`, true);
  } finally {
    state.saving = false;
    button.disabled = false;
    button.textContent = "保存配置";
  }
}

async function refreshSessions() {
  const button = $("refresh-sessions");
  button.disabled = true;
  const hint = $("refresh-hint");
  hint.textContent = "正在拉取…";
  try {
    const data = await api.post(PLUGIN_ENDPOINT.refresh, { remote: true });
    state.sessions = data.sessions || [];
    state.platforms = data.platforms || [];
    state.messageTypes = data.message_types || [];
    state.overview = data.overview || state.overview;
    renderFilters();
    renderSessions();
    hint.textContent = `已刷新：${state.sessions.length} 个会话`;
  } catch (error) {
    hint.textContent = "";
    toast(`刷新失败：${error.message || error}`, true);
  } finally {
    button.disabled = false;
    window.setTimeout(() => {
      hint.textContent = "";
    }, 4000);
  }
}

/* ------------------------------------------------------------------ */
/* 数据加载                                                            */
/* ------------------------------------------------------------------ */
function applyPayload(data) {
  if (!data || typeof data !== "object") return;
  state.settings = data.settings || state.settings;
  state.sessions = data.sessions || state.sessions || [];
  state.platforms = data.platforms || [];
  state.messageTypes = data.message_types || [];
  state.overview = data.overview || {};
  state.schedules = (state.settings?.schedules || []).map((task) => ({ ...task }));
  state.runtime = Object.fromEntries(
    (state.overview?.schedules || []).map((item) => [item.id, item])
  );
  renderAll();
}

function bindEvents() {
  $("save").addEventListener("click", save);
  $("refresh-sessions").addEventListener("click", refreshSessions);
  $("add-schedule").addEventListener("click", addSchedule);
  $("clear-finished").addEventListener("click", clearFinished);

  $("search").addEventListener("input", () => {
    state.filters.q = $("search").value;
    renderSessions();
  });
  $("filter-platform").addEventListener("change", () => {
    state.filters.platform = $("filter-platform").value;
    renderSessions();
  });
  $("filter-type").addEventListener("change", () => {
    state.filters.type = $("filter-type").value;
    renderSessions();
  });

  document.querySelectorAll("#status-tabs button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.filters.status = btn.dataset.status;
      document.querySelectorAll("#status-tabs button").forEach((item) => {
        item.classList.toggle("active", item === btn);
      });
      renderSessions();
    });
  });

  $("select-all").addEventListener("click", () => {
    const set = targetSet();
    filteredSessions().forEach((session) => set.add(session.umo));
    applyTargets(set);
    toast(`已选中 ${set.size} 个会话`);
  });

  $("clear-all").addEventListener("click", () => {
    if (targetSet().size === 0) {
      toast("当前没有勾选任何会话");
      return;
    }
    applyTargets(new Set());
  });

  ["send-interval", "max-per-minute", "daily-limit"].forEach((id) => {
    $(id).addEventListener("input", () => {
      if (state.settings) {
        const key = id.replace(/-/g, "_");
        const aliases = { send_interval: "send_interval", max_per_minute: "max_per_minute", daily_limit: "daily_limit" };
        state.settings[aliases[key] || key] = Number($(id).value || 0);
        updateSaveSummary();
      }
    });
  });
}

async function boot() {
  try {
    state.bridge = await waitForBridge();
    const context = await state.bridge.ready();
    document.documentElement.setAttribute(
      "data-theme",
      context && context.isDark ? "dark" : "light"
    );
    state.bridge.onContext?.((next) => {
      document.documentElement.setAttribute(
        "data-theme",
        next && next.isDark ? "dark" : "light"
      );
    });

    bindEvents();
    const data = await api.get(PLUGIN_ENDPOINT.get);
    applyPayload(data);
    $("loading").classList.add("hidden");
    $("content").classList.remove("hidden");
  } catch (error) {
    $("loading").textContent = `加载失败：${error.message || error}`;
    $("loading").style.color = "var(--danger)";
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
