/* ===========================================================
 * 闲鱼低价提醒工具 —— Web 基础版前端逻辑（P1）
 * 纯原生 JS（无 node 构建链）：fetch 封装、Tab 渲染、表格/弹窗
 * 交互、SSE 实时日志、状态条轮询。
 * =========================================================== */

"use strict";

/* ---------------- 全局状态 ---------------- */
const state = {
  config: null,          // GET /api/config 返回的表单
  channelOrder: ["console", "serverchan", "email", "telegram", "bark", "webhook"],
  channelLabels: {
    console: "控制台（打印到容器日志，永远可用）",
    serverchan: "Server酱（微信推送）",
    email: "邮件（SMTP）",
    telegram: "Telegram Bot",
    bark: "Bark（iOS 推送）",
    webhook: "企业微信机器人（Webhook）",
  },
  channelFields: {
    console: [],
    serverchan: [["sendkey", "SendKey", true, ""]],
    email: [
      ["smtp_host", "SMTP 服务器", false, "smtp.qq.com"],
      ["smtp_port", "端口（465=SSL / 587=TLS）", false, "465"],
      ["username", "账号（同时作为发件人）", false, ""],
      ["password", "密码 / 授权码", true, ""],
      ["to", "收件人（多个用英文逗号分隔）", false, ""],
    ],
    telegram: [
      ["bot_token", "Bot Token", true, ""],
      ["chat_id", "Chat ID", false, ""],
    ],
    bark: [["url", "Bark URL（形如 https://api.day.app/YourKey/）", false, "https://api.day.app/"]],
    webhook: [["url", "Webhook URL（企业微信群机器人地址）", false, ""]],
  },
  recordSort: "time",
  recordOrder: "desc",
};

/* ---------------- 工具函数 ---------------- */
function $(sel) {
  return document.querySelector(sel);
}

async function api(path, options = {}) {
  const opts = {
    method: options.method || "GET",
    headers: { "Content-Type": "application/json" },
  };
  if (options.body !== undefined) {
    opts.body = JSON.stringify(options.body);
  }
  const resp = await fetch(path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch (_e) {
    data = null;
  }
  if (!resp.ok || (data && data.ok === false)) {
    const message = (data && data.message) || `HTTP ${resp.status}`;
    throw new Error(message);
  }
  return data || {};
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
}

function escapeHtml(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function filterSummary(filters) {
  const f = filters || {};
  const parts = [];
  if (f.exclude_keywords && f.exclude_keywords.length) {
    parts.push("排除:" + f.exclude_keywords.join(","));
  }
  if (f.required_keywords && f.required_keywords.length) {
    parts.push("必含:" + f.required_keywords.join(","));
  }
  return parts.length ? parts.join(" ") : "—";
}

/* ---------------- Tab 切换 ---------------- */
function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((p) => {
    p.classList.toggle("active", p.id === "panel-" + name);
  });
}

function initTabs() {
  document.querySelectorAll(".tab").forEach((t) => {
    t.addEventListener("click", () => switchTab(t.dataset.tab));
  });
}

/* ===========================================================
 * Tab 1：监控配置
 * =========================================================== */
function renderKeywordTable() {
  const tbody = $("#kw-tbody");
  const keywords = state.config.keywords || [];
  if (!keywords.length) {
    tbody.innerHTML =
      '<tr class="empty-row"><td colspan="5">还没有关键词，请在上方输入后点击「添加」</td></tr>';
    return;
  }
  tbody.innerHTML = keywords
    .map(
      (k, idx) => `
    <tr data-idx="${idx}">
      <td>${escapeHtml(k.keyword)}</td>
      <td>¥ ${escapeHtml(k.max_price)}</td>
      <td class="filter-text">${escapeHtml(filterSummary(k))}</td>
      <td>
        <label class="switch" title="启用/停用">
          <input type="checkbox" data-action="toggle" ${k.enabled !== false ? "checked" : ""}>
          <span class="slider"></span>
        </label>
        <span class="filter-text">${k.enabled !== false ? "启用" : "停用"}</span>
      </td>
      <td>
        <div class="row-actions">
          <button class="btn small" data-action="filter" type="button">过滤词</button>
          <button class="btn small danger" data-action="delete" type="button">删除</button>
        </div>
      </td>
    </tr>`
    )
    .join("");
}

function addKeyword() {
  const kw = $("#kw-input").value.trim();
  const price = parseFloat($("#price-input").value);
  if (!kw) {
    toast("关键词不能为空", true);
    return;
  }
  if (!isFinite(price) || price <= 0) {
    toast("价格阈值必须为正数", true);
    return;
  }
  state.config.keywords = state.config.keywords || [];
  if (state.config.keywords.some((k) => k.keyword === kw)) {
    toast("关键词已存在，请直接编辑该行", true);
    return;
  }
  state.config.keywords.push({
    keyword: kw,
    max_price: price,
    enabled: true,
    exclude_keywords: [],
    required_keywords: [],
  });
  $("#kw-input").value = "";
  $("#price-input").value = "";
  renderKeywordTable();
}

function editFilter(idx) {
  const k = state.config.keywords[idx];
  const excludeText = (k.exclude_keywords || []).join("\n");
  const requiredText = (k.required_keywords || []).join("\n");
  const newExclude = prompt(`编辑「${k.keyword}」的排除词（每行一个，空=不排除）：`, excludeText);
  if (newExclude === null) return;
  const newRequired = prompt(`编辑「${k.keyword}」的必含词（每行一个，空=不强制）：`, requiredText);
  if (newRequired === null) return;
  k.exclude_keywords = newExclude.split("\n").map((s) => s.trim()).filter(Boolean);
  k.required_keywords = newRequired.split("\n").map((s) => s.trim()).filter(Boolean);
  renderKeywordTable();
}

function initConfigTab() {
  $("#kw-add-btn").addEventListener("click", addKeyword);
  $("#kw-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addKeyword();
  });
  $("#kw-tbody").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const tr = btn.closest("tr[data-idx]");
    if (!tr) return;
    const idx = Number(tr.dataset.idx);
    const action = btn.dataset.action;
    if (action === "delete") {
      state.config.keywords.splice(idx, 1);
      renderKeywordTable();
    } else if (action === "filter") {
      editFilter(idx);
    }
  });
  $("#kw-tbody").addEventListener("change", (e) => {
    const input = e.target.closest('input[data-action="toggle"]');
    if (!input) return;
    const tr = input.closest("tr[data-idx]");
    if (!tr) return;
    const idx = Number(tr.dataset.idx);
    state.config.keywords[idx].enabled = input.checked;
    renderKeywordTable();
  });

  $("#cookie-save-btn").addEventListener("click", saveCookie);
  $("#cookie-clear-btn").addEventListener("click", () => {
    $("#cookie-input").value = "";
  });

  $("#config-save-btn").addEventListener("click", saveConfig);
}

function collectForm() {
  const config = state.config;
  config.interval_seconds = parseInt($("#interval-input").value, 10) || 600;
  config.pages = parseInt($("#pages-input").value, 10) || 1;
  config.fetcher_type = $("#fetcher-select").value;
  config.cookie_alert_enabled = $("#cookie-alert-input").checked;
  config.cookie_check_interval_seconds =
    parseInt($("#cookie-check-interval-input").value, 10) || 0;
  return config;
}

async function saveConfig() {
  const statusEl = $("#config-save-status");
  statusEl.textContent = "保存中…";
  try {
    const form = collectForm();
    const result = await api("/api/config", { method: "PUT", body: { form } });
    statusEl.textContent = result.restarted ? "已保存并热重启 ✓" : "已保存 ✓";
    toast(result.message || "配置已保存并生效");
    await loadConfig();
  } catch (err) {
    statusEl.textContent = "保存失败 ✗";
    toast("保存失败：" + err.message, true);
  }
}

async function loadConfig() {
  try {
    const data = await api("/api/config");
    state.config = data;
    renderKeywordTable();
    $("#interval-input").value = data.interval_seconds || 600;
    $("#pages-input").value = data.pages || 1;
    $("#fetcher-select").value = data.fetcher_type || "mtop";
    $("#cookie-alert-input").checked = data.cookie_alert_enabled !== false;
    $("#cookie-check-interval-input").value = data.cookie_check_interval_seconds || 0;
    renderCookieStatus(data);
    renderChannels(data);
  } catch (err) {
    toast("加载配置失败：" + err.message, true);
  }
}

function renderCookieStatus(data) {
  const health = data.cookie_health || { state: "unknown", text: "未知" };
  const light = $("#cookie-status-light");
  light.className = "status-light " + (health.state || "unknown");
  light.title = health.text || "";
  $("#cookie-status-text").textContent = health.text || "—";
  $("#cookie-masked").textContent = data.cookies_masked ? "（已保存：" + data.cookies_masked + "）" : "";
}

async function saveCookie() {
  const cookie = $("#cookie-input").value.trim();
  if (!cookie) {
    toast("请先粘贴 Cookie", true);
    return;
  }
  const btn = $("#cookie-save-btn");
  btn.disabled = true;
  btn.textContent = "校验中…";
  try {
    const result = await api("/api/cookie/save", {
      method: "POST",
      body: { cookie },
    });
    toast(result.message || "Cookie 已更新并加密保存");
    $("#cookie-input").value = "";
    await loadConfig();
  } catch (err) {
    toast("Cookie 保存失败：" + err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "校验并保存";
  }
}

/* ===========================================================
 * Tab 2：通知设置
 * =========================================================== */
function renderChannels(data) {
  const channels = data.channels || {};
  const list = $("#channel-list");
  list.innerHTML = state.channelOrder
    .map((ctype) => {
      const ch = channels[ctype] || { enabled: false, options: {} };
      const options = ch.options || {};
      const fields = state.channelFields[ctype] || [];
      const fieldHtml = fields
        .map(([key, label, isSecret, def]) => {
          const value = options[key] !== undefined ? options[key] : def;
          return `<label>${escapeHtml(label)}
            <input type="${isSecret ? "password" : "text"}" data-ctype="${ctype}" data-field="${key}"
              value="${escapeHtml(value)}" autocomplete="off"></label>`;
        })
        .join("");
      return `
      <div class="channel-card ${ch.enabled ? "enabled" : ""}" data-ctype="${ctype}">
        <div class="channel-head">
          <span class="channel-name">${escapeHtml(state.channelLabels[ctype] || ctype)}</span>
          <label class="switch">
            <input type="checkbox" data-ctype="${ctype}" data-action="channel-toggle"
              ${ch.enabled ? "checked" : ""}>
            <span class="slider"></span>
          </label>
        </div>
        <div class="channel-fields">${fieldHtml}</div>
        <div class="channel-actions">
          <button class="btn small" data-ctype="${ctype}" data-action="channel-test" type="button">测试发送</button>
          <span class="channel-test-result" data-ctype="${ctype}"></span>
        </div>
      </div>`;
    })
    .join("");
}

function channelOptions(ctype) {
  const options = {};
  document.querySelectorAll(`.channel-card[data-ctype="${ctype}"] input[data-field]`).forEach((input) => {
    options[input.dataset.field] = input.value.trim();
  });
  return options;
}

function initNotifyTab() {
  $("#channel-list").addEventListener("change", (e) => {
    const input = e.target.closest('input[data-action="channel-toggle"]');
    if (!input) return;
    const card = input.closest(".channel-card");
    card.classList.toggle("enabled", input.checked);
    // 同步到 state.config.channels（保存时用）
    const ctype = input.dataset.ctype;
    state.config.channels = state.config.channels || {};
    state.config.channels[ctype] = state.config.channels[ctype] || { enabled: false, options: {} };
    state.config.channels[ctype].enabled = input.checked;
  });

  $("#channel-list").addEventListener("input", (e) => {
    const input = e.target.closest('input[data-field]');
    if (!input) return;
    const ctype = input.dataset.ctype;
    state.config.channels = state.config.channels || {};
    state.config.channels[ctype] = state.config.channels[ctype] || { enabled: false, options: {} };
    state.config.channels[ctype].options = channelOptions(ctype);
  });

  $("#channel-list").addEventListener("click", async (e) => {
    const btn = e.target.closest('button[data-action="channel-test"]');
    if (!btn) return;
    const ctype = btn.dataset.ctype;
    const resultEl = document.querySelector(`.channel-test-result[data-ctype="${ctype}"]`);
    resultEl.textContent = "发送中…";
    btn.disabled = true;
    try {
      await api("/api/notify/test", {
        method: "POST",
        body: { channel_type: ctype, options: channelOptions(ctype) },
      });
      resultEl.textContent = "✓ 已发送";
    } catch (err) {
      resultEl.textContent = "✗ " + err.message;
    } finally {
      btn.disabled = false;
    }
  });
}

/* ===========================================================
 * Tab 3：运行监控
 * =========================================================== */
function initRunTab() {
  $("#monitor-start-btn").addEventListener("click", () => monitorAction("start"));
  $("#monitor-stop-btn").addEventListener("click", () => monitorAction("stop"));
  $("#monitor-once-btn").addEventListener("click", () => monitorAction("run_once"));
  $("#records-refresh-btn").addEventListener("click", loadRecords);
  $("#include-sold-input").addEventListener("change", loadRecords);
  $("#record-table").addEventListener("click", (e) => {
    const th = e.target.closest("th[data-sort]");
    if (th) {
      const sort = th.dataset.sort;
      if (state.recordSort === sort) {
        state.recordOrder = state.recordOrder === "asc" ? "desc" : "asc";
      } else {
        state.recordSort = sort;
        state.recordOrder = "asc";
      }
      loadRecords();
    }
    const btn = e.target.closest("button[data-action]");
    if (!btn) return;
    const tr = btn.closest("tr[data-id]");
    if (!tr) return;
    const id = tr.dataset.id;
    if (btn.dataset.action === "sold") {
      recordAction(`/api/records/${encodeURIComponent(id)}/sold`, "POST", "已标记为已下架");
    } else if (btn.dataset.action === "blacklist") {
      recordAction(`/api/records/${encodeURIComponent(id)}/blacklist`, "POST", "已加入黑名单");
    }
  });
  // 启动 SSE 日志流
  connectSse();
  // 状态条轮询 2s
  setInterval(pollStatus, 2000);
  pollStatus();
  loadRecords();
}

async function monitorAction(action) {
  try {
    const result = await api("/api/monitor/" + action, { method: "POST" });
    toast(result.message || "操作成功");
    pollStatus();
  } catch (err) {
    toast("操作失败：" + err.message, true);
  }
}

async function pollStatus() {
  try {
    const data = await api("/api/monitor/status");
    const running = data.running;
    $("#monitor-status-text").textContent = running ? "状态：运行中" : "状态：未运行";
    $("#stat-rounds").textContent = data.round_count || 0;
    $("#stat-notified").textContent = data.notified_count || 0;
    $("#stat-last-round").textContent = data.last_round_at || "—";
    $("#stat-next-round").textContent =
      running && data.next_round_in != null ? `${data.next_round_in}s` : "—";
  } catch (_err) {
    /* 轮询失败静默，下一轮重试 */
  }
}

/* ---------------- SSE 实时日志 ---------------- */
function connectSse() {
  const logBox = $("#log-box");
  const source = new EventSource("/api/logs/stream");
  source.onopen = () => {
    logBox.querySelector(".log-empty")?.remove();
  };
  source.onmessage = (event) => {
    let entry;
    try {
      entry = JSON.parse(event.data);
    } catch (_e) {
      return;
    }
    appendLogLine(entry);
  };
  source.onerror = () => {
    // 断线自动重连（EventSource 原生行为），显示提示后由 onopen 清理
    if (!logBox.querySelector(".log-reconnecting")) {
      const div = document.createElement("div");
      div.className = "log-line DIM log-reconnecting";
      div.textContent = "（日志连接中断，正在重连…）";
      logBox.appendChild(div);
    }
  };
}

function appendLogLine(entry) {
  const logBox = $("#log-box");
  const level = entry.level || "INFO";
  // 复用 gui.log_tag_for_text 的语义：前端按文本前缀着色
  const text = entry.text || "";
  let tag = level;
  if (text.includes("🔔") || text.includes("命中低价") || text.includes("新出现")) {
    tag = "NEW_ITEM";
  } else if (text.includes("✅") || text.includes("本轮完成") || text.includes("已保存") || text.includes("已启动")) {
    tag = "SUMMARY";
  } else if (text.includes("🚫") || text.includes("已停用")) {
    tag = "DIM";
  } else if (text.includes("=====") || text.includes("轮监测开始")) {
    tag = "ROUND";
  }
  const div = document.createElement("div");
  div.className = "log-line " + tag;
  div.textContent = text;
  logBox.appendChild(div);
  // 滚动到底部；限制 DOM 行数（对齐 MAX_LOG_LINES=2000）
  while (logBox.childElementCount > 2000) {
    logBox.removeChild(logBox.firstChild);
  }
  logBox.scrollTop = logBox.scrollHeight;
}

/* ---------------- 提醒记录 ---------------- */
async function loadRecords() {
  try {
    const includeSold = $("#include-sold-input").checked;
    const data = await api(
      `/api/records?include_sold=${includeSold}&sort=${state.recordSort}&order=${state.recordOrder}`
    );
    renderRecords(data.records || []);
  } catch (err) {
    toast("加载提醒记录失败：" + err.message, true);
  }
}

function renderRecords(records) {
  const tbody = $("#record-tbody");
  $("#records-count").textContent = records.length ? `共 ${records.length} 条` : "";
  if (!records.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="6">暂无提醒记录</td></tr>';
    return;
  }
  tbody.innerHTML = records
    .map((r) => {
      const url = r.url ? `<a href="${escapeHtml(r.url)}" target="_blank" rel="noopener">${escapeHtml(r.title)}</a>` : escapeHtml(r.title);
      return `
      <tr data-id="${escapeHtml(r.product_id)}">
        <td class="nowrap">${escapeHtml(r.time || "")}</td>
        <td>${escapeHtml(r.keyword || "")}</td>
        <td class="record-title">${url}</td>
        <td class="price-cell">¥ ${Number(r.price || 0).toFixed(2)}</td>
        <td>${escapeHtml(r.publish || "")}</td>
        <td>
          <div class="row-actions">
            <button class="btn small" data-action="sold" type="button">标记已下架</button>
            <button class="btn small danger" data-action="blacklist" type="button">黑名单</button>
          </div>
        </td>
      </tr>`;
    })
    .join("");
}

async function recordAction(path, method, successMsg) {
  try {
    await api(path, { method });
    toast(successMsg);
    loadRecords();
  } catch (err) {
    toast("操作失败：" + err.message, true);
  }
}

/* ===========================================================
 * 初始化
 * =========================================================== */
async function init() {
  initTabs();
  initConfigTab();
  initNotifyTab();
  initRunTab();
  try {
    const version = await api("/healthz");
    $("#app-version").textContent = "v1.8.0";
  } catch (_e) {
    /* 版本号展示失败不阻塞 */
  }
  await loadConfig();
}

document.addEventListener("DOMContentLoaded", init);
