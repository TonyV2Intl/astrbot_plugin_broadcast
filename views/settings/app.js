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
const MAX_RENDER_ROWS = 400;

const state = {
  bridge: null,
  settings: null,
  sessions: [],
  platforms: [],
  messageTypes: [],
  overview: {},
  filters: { q: "", platform: "", type: "", status: "all" },
  scheduleMode: "daily",
  weekdays: [0, 1, 2, 3, 4, 5, 6],
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

function scheduleText(mode, time, weekdays) {
  if (mode === "weekly") {
    const days = [...weekdays].sort((a, b) => a - b).map((d) => WEEKDAY_LABELS[d]);
    return `每周 ${days.join("、") || "（未选择）"} ${time}`;
  }
  return `每天 ${time}`;
}

function cronText(mode, time, weekdays) {
  const [hour, minute] = String(time || "09:00").split(":");
  const dow =
    mode === "weekly"
      ? [...weekdays].sort((a, b) => a - b).join(",") || "*"
      : "*";
  return `${Number(minute)} ${Number(hour)} * * ${dow}`;
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
  const paths =
    kind === "restore"
      ? ["M9 14 4 9l5-5", "M4 9h11a5 5 0 0 1 0 10h-3"]
      : ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M5.6 5.6l12.8 12.8"];
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

function excludeSet() {
  return new Set(state.settings?.exclude_umos || []);
}

/* ------------------------------------------------------------------ */
/* 渲染                                                                */
/* ------------------------------------------------------------------ */
function renderOverview() {
  const overview = state.overview || {};
  const settings = state.settings || {};
  const sessions = state.sessions || [];
  const excluded = excludeSet();
  const targets = targetSet();

  const eligible = sessions.filter((s) => {
    if (excluded.has(s.umo)) return false;
    if (targets.size === 0) return true;
    return targets.has(s.umo);
  }).length;

  $("stat-total").textContent = String(sessions.length);
  $("stat-target").innerHTML = `${targets.size > 0 ? targets.size : eligible}` +
    `<small> / ${sessions.length}</small>`;
  $("stat-quota").innerHTML =
    settings.daily_limit > 0
      ? `${overview.today_sent ?? 0}<small> / ${settings.daily_limit}</small>`
      : `${overview.today_sent ?? 0}<small> / 不限</small>`;
  $("stat-next").textContent = overview.schedule_active
    ? formatDateTime(overview.next_run_at)
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
  } else if (overview.schedule_active) {
    banner.className = "banner ok";
    banner.textContent = `定时广播已生效：${settings.schedule?.mode === "weekly" ? "每周" : "每天"} ${
      settings.schedule?.time || ""
    }，下次触发 ${formatDateTime(overview.next_run_at)}`;
  } else {
    banner.className = "banner hidden";
    banner.textContent = "";
  }
}

function renderModeBanner() {
  const banner = $("mode-banner");
  const excluded = excludeSet();
  const targets = targetSet();
  const total = state.sessions.filter((s) => !excluded.has(s.umo)).length;

  if (targets.size === 0) {
    banner.className = "banner";
    banner.innerHTML = `当前模式：<strong>全部会话</strong>。未勾选任何会话，将向 ${total} 个可用会话广播。勾选后切换为“仅向选中会话”模式。`;
  } else {
    const effective = [...targets].filter((umo) => !excluded.has(umo)).length;
    banner.className = "banner ok";
    banner.innerHTML = `当前模式：<strong>仅向选中会话</strong>。已选 ${targets.size} 个，实际可发送 ${effective} 个。`;
  }
}

function renderWeekdays() {
  const box = $("weekdays");
  box.innerHTML = "";
  WEEKDAY_LABELS.forEach((label, index) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.dataset.day = String(index);
    if (state.weekdays.includes(index)) btn.classList.add("active");
    btn.addEventListener("click", () => {
      const set = new Set(state.weekdays);
      if (set.has(index)) set.delete(index);
      else set.add(index);
      state.weekdays = [...set].sort((a, b) => a - b);
      renderWeekdays();
      renderSchedulePreview();
    });
    box.appendChild(btn);
  });
}

function renderSchedule() {
  const settings = state.settings || {};
  $("enable-schedule").checked = Boolean(settings.enable_schedule);
  $("enable-schedule-label").textContent = settings.enable_schedule ? "开启" : "关闭";

  const schedule = settings.schedule || {};
  state.scheduleMode = schedule.mode === "weekly" ? "weekly" : "daily";
  state.weekdays =
    Array.isArray(schedule.weekdays) && schedule.weekdays.length
      ? schedule.weekdays
      : [0, 1, 2, 3, 4, 5, 6];

  document.querySelectorAll("#schedule-mode button").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === state.scheduleMode);
  });
  $("weekday-field").classList.toggle("hidden", state.scheduleMode !== "weekly");
  $("schedule-time").value = schedule.time || "09:00";
  $("schedule-message").value = settings.schedule_message || "";
  renderWeekdays();
  renderSchedulePreview();
}

function renderSchedulePreview() {
  const mode = state.scheduleMode;
  const time = $("schedule-time").value || "09:00";
  const enabled = $("enable-schedule").checked;
  const length = $("schedule-message").value.length;
  const head = enabled ? scheduleText(mode, time, state.weekdays) : "定时广播未启用";
  $("schedule-preview").textContent = `${head}　·　Cron ${cronText(
    mode,
    time,
    state.weekdays
  )}　·　正文 ${length} 字`;
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
  const excluded = excludeSet();

  return state.sessions.filter((session) => {
    if (platform && session.platform !== platform) return false;
    if (type && session.message_type !== type) return false;

    const selected = targets.size === 0 ? !excluded.has(session.umo) : targets.has(session.umo);
    if (status === "selected" && !selected) return false;
    if (status === "unselected" && selected) return false;
    if (status === "excluded" && !excluded.has(session.umo)) return false;

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
  const excluded = excludeSet();
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
      const isExcluded = excluded.has(session.umo);
      const isSelected =
        targets.size === 0 ? !isExcluded : targets.has(session.umo);

      const row = document.createElement("div");
      row.className = "session-row" + (isExcluded ? " excluded" : "");
      row.dataset.umo = session.umo;

      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = isSelected;
      checkbox.disabled = isExcluded;
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

      const excludeBtn = document.createElement("button");
      excludeBtn.type = "button";
      excludeBtn.className = "icon-btn";
      excludeBtn.title = isExcluded ? "取消排除" : "永不向该会话广播";
      excludeBtn.appendChild(makeIcon(isExcluded ? "restore" : "ban"));
      excludeBtn.addEventListener("click", (event) => {
        event.stopPropagation();
        toggleExclude(session.umo);
      });

      row.append(checkbox, main, platformBadge, typeBadge, excludeBtn);
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
  $("enabled").checked = Boolean(state.overview?.enabled);
  $("enabled-label").textContent = state.overview?.enabled ? "已启用" : "已禁用";
  $("admin-uids").value = (settings.admin_uids || []).join(", ");
  $("send-interval").value = settings.send_interval ?? 2;
  $("max-per-minute").value = settings.max_per_minute ?? 20;
  $("daily-limit").value = settings.daily_limit ?? 1000;
  $("timezone").value = settings.timezone || "Asia/Shanghai";
  $("auto-register").checked = settings.auto_register !== false;
  $("auto-register-label").textContent = settings.auto_register === false ? "关闭" : "开启";

  renderFilters();
  renderSchedule();
  renderSessions();
  renderLastResult();
}

function updateSaveSummary() {
  const settings = state.settings || {};
  const targets = targetSet();
  const mode = targets.size ? `仅发 ${targets.size} 个会话` : "全部会话";
  const schedule = settings.enable_schedule
    ? scheduleText(settings.schedule?.mode, settings.schedule?.time, state.weekdays)
    : "定时未开启";
  $("save-summary").textContent = `${mode}　·　${schedule}　·　间隔 ${settings.send_interval}s`;
}

/* ------------------------------------------------------------------ */
/* 交互                                                                */
/* ------------------------------------------------------------------ */
function toggleInclude(umo, checked) {
  const excluded = excludeSet();
  let set = targetSet();

  if (set.size === 0) {
    // 从“全部”模式物化：先把当前所有可用会话视为已选
    state.sessions.forEach((session) => {
      if (!excluded.has(session.umo)) set.add(session.umo);
    });
  }

  if (checked) {
    if (excluded.has(umo)) return;
    set.add(umo);
  } else {
    set.delete(umo);
  }

  applyTargets(set);
}

function applyTargets(set) {
  if (set.size === 0) {
    state.settings.target_umos = [];
    toast("已取消全部选择：当前将向全部会话广播");
  } else {
    const ordered = [];
    state.sessions.forEach((session) => {
      if (set.has(session.umo)) ordered.push(session.umo);
    });
    set.forEach((umo) => {
      if (!ordered.includes(umo)) ordered.push(umo);
    });
    state.settings.target_umos = ordered;
  }
  renderSessions();
}

function toggleExclude(umo) {
  const set = excludeSet();
  if (set.has(umo)) {
    set.delete(umo);
  } else {
    set.add(umo);
    const targets = targetSet();
    if (targets.has(umo)) {
      targets.delete(umo);
      state.settings.target_umos = [...targets];
    }
  }
  state.settings.exclude_umos = [...set];
  renderSessions();
}

function collectForm() {
  const settings = state.settings || {};
  return {
    enabled: $("enabled").checked,
    admin_uids: splitList($("admin-uids").value),
    target_umos: settings.target_umos || [],
    exclude_umos: settings.exclude_umos || [],
    send_interval: Number($("send-interval").value || 0),
    max_per_minute: Number($("max-per-minute").value || 0),
    daily_limit: Number($("daily-limit").value || 0),
    enable_schedule: $("enable-schedule").checked,
    schedule: {
      mode: state.scheduleMode,
      time: $("schedule-time").value || "09:00",
      weekdays: [...state.weekdays].sort((a, b) => a - b),
    },
    schedule_message: $("schedule-message").value || "",
    timezone: $("timezone").value.trim() || "Asia/Shanghai",
    auto_register: $("auto-register").checked,
  };
}

async function save() {
  if (state.saving) return;
  state.saving = true;
  const button = $("save");
  button.disabled = true;
  button.textContent = "保存中…";
  try {
    const payload = collectForm();
    if (payload.enable_schedule && !payload.schedule_message.trim()) {
      toast("已开启定时广播，但消息内容为空，到点不会发送", true);
    }
    const data = await api.post(PLUGIN_ENDPOINT.save, payload);
    applyPayload(data);
    toast(data.schedule_ok ? "已保存，定时任务已生效" : `已保存：${data.schedule_message}`);
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
  renderAll();
}

function bindEvents() {
  $("save").addEventListener("click", save);
  $("refresh-sessions").addEventListener("click", refreshSessions);

  $("enabled").addEventListener("change", () => {
    $("enabled-label").textContent = $("enabled").checked ? "已启用" : "已禁用";
  });
  $("auto-register").addEventListener("change", () => {
    $("auto-register-label").textContent = $("auto-register").checked ? "开启" : "关闭";
  });
  $("enable-schedule").addEventListener("change", () => {
    $("enable-schedule-label").textContent = $("enable-schedule").checked ? "开启" : "关闭";
    renderSchedulePreview();
  });

  document.querySelectorAll("#schedule-mode button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.scheduleMode = btn.dataset.mode;
      document.querySelectorAll("#schedule-mode button").forEach((item) => {
        item.classList.toggle("active", item === btn);
      });
      $("weekday-field").classList.toggle("hidden", state.scheduleMode !== "weekly");
      renderSchedulePreview();
    });
  });

  $("schedule-time").addEventListener("change", renderSchedulePreview);
  $("schedule-message").addEventListener("input", renderSchedulePreview);

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
    filteredSessions().forEach((session) => {
      if (!excludeSet().has(session.umo)) set.add(session.umo);
    });
    applyTargets(set);
    toast(`已选中 ${set.size} 个会话`);
  });

  $("clear-all").addEventListener("click", () => {
    if (targetSet().size === 0) {
      toast("当前已经是全部会话模式");
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
