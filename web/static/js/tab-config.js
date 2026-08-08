/* ===========================================================
 * 闲鱼低价提醒工具 —— tab-config.js（Tab1 监控配置）
 * 挂载到 window.XY.TabConfig。
 *
 * 职责（P2-01/P2-06/P2-08/P2-09）：
 *   - 关键词表格：添加 / 行内编辑（改词改价）/ 删除 / 启停
 *   - 过滤词弹窗（排除词/必含词，替换 prompt()）
 *   - 预置排除词编辑弹窗（写回 config 顶层 preset_exclude_keywords）
 *   - 监测参数表单：interval / pages / page_size / page_sleep /
 *     user_agent / fetcher_type / cookie_alert / cookie_check_interval
 *   - Cookie 状态灯 + 单值粘贴保存
 *   - Cookie 池管理弹窗（add/update/delete/toggle/set_default/
 *     refresh_selected/auto_disable_expired/检测全部/帮助）
 *   - 保存配置（校验 → PUT /api/config）
 * =========================================================== */
window.XY = window.XY || {};
window.XY.TabConfig = (function () {
  "use strict";
  const U = window.XY.Util;
  const Api = window.XY.Api;
  const Modal = window.XY.Modal;
  const State = window.XY.State;

  // ------------------------------------------------------------------ //
  // 关键词表格
  // ------------------------------------------------------------------ //
  function renderKeywordTable() {
    const tbody = U.$("#kw-tbody");
    const keywords = (State.config && State.config.keywords) || [];
    if (!keywords.length) {
      tbody.innerHTML =
        '<tr class="empty-row"><td colspan="5">还没有关键词，请在上方输入后点击「添加」</td></tr>';
      return;
    }
    tbody.innerHTML = keywords
      .map(
        (k, idx) => `
    <tr data-idx="${idx}">
      <td>${U.escapeHtml(k.keyword)}</td>
      <td>¥ ${U.escapeHtml(k.max_price)}</td>
      <td class="filter-text">${U.escapeHtml(U.filterSummary(k))}</td>
      <td>
        <label class="switch" title="启用/停用">
          <input type="checkbox" data-action="toggle" ${k.enabled !== false ? "checked" : ""}>
          <span class="slider"></span>
        </label>
        <span class="filter-text">${k.enabled !== false ? "启用" : "停用"}</span>
      </td>
      <td>
        <div class="row-actions">
          <button class="btn small" data-action="edit" type="button">编辑</button>
          <button class="btn small" data-action="filter" type="button">过滤词</button>
          <button class="btn small danger" data-action="delete" type="button">删除</button>
        </div>
      </td>
    </tr>`
      )
      .join("");
  }

  function addKeyword() {
    const kwInput = U.$("#kw-input");
    const priceInput = U.$("#price-input");
    const kw = kwInput.value.trim();
    const price = parseFloat(priceInput.value);
    if (!kw) {
      U.fieldError(kwInput, "关键词不能为空");
      return;
    }
    if (!isFinite(price) || price <= 0) {
      U.fieldError(priceInput, "价格阈值必须为正数");
      return;
    }
    State.config.keywords = State.config.keywords || [];
    if (State.config.keywords.some((k) => k.keyword === kw)) {
      U.fieldError(kwInput, "关键词已存在，请直接编辑该行");
      return;
    }
    State.config.keywords.push({
      keyword: kw,
      max_price: price,
      enabled: true,
      exclude_keywords: [],
      required_keywords: [],
    });
    kwInput.value = "";
    priceInput.value = "";
    renderKeywordTable();
  }

  /** 行内编辑（改词/改价，P2-06）：Modal.prompt 替代删除重建。 */
  function editKeyword(idx) {
    const k = State.config.keywords[idx];
    if (!k) return;
    Modal.prompt({
      title: `编辑关键词「${k.keyword}」`,
      fields: [
        { key: "keyword", label: "关键词", type: "text", value: k.keyword, required: true },
        { key: "max_price", label: "价格阈值（元）", type: "number", value: k.max_price, required: true },
      ],
      validate(values) {
        const kw = String(values.keyword || "").trim();
        const price = parseFloat(values.max_price);
        if (!kw) return "关键词不能为空";
        if (!isFinite(price) || price <= 0) return "价格阈值必须为正数";
        if (kw !== k.keyword && State.config.keywords.some((x) => x.keyword === kw)) {
          return "关键词已存在，请改用其它名称";
        }
        return null;
      },
      onSave(values) {
        k.keyword = String(values.keyword || "").trim();
        k.max_price = parseFloat(values.max_price);
        renderKeywordTable();
        U.toast("关键词已更新，记得保存配置生效");
      },
    });
  }

  /** 过滤词弹窗（P2-06）：排除词 / 必含词 多行输入 + 当前关键词名。 */
  function editFilter(idx) {
    const k = State.config.keywords[idx];
    if (!k) return;
    Modal.open({
      title: `编辑「${k.keyword}」的过滤词`,
      width: "480px",
      bodyHtml:
        '<form class="modal-form" id="filter-form">' +
        "<label>排除词（每行一个，标题命中任一即跳过）" +
        `<textarea id="filter-exclude" rows="6">${U.escapeHtml((k.exclude_keywords || []).join("\n"))}</textarea></label>` +
        "<label>必含词（每行一个，标题必须全部包含；留空=不强制）" +
        `<textarea id="filter-required" rows="4">${U.escapeHtml((k.required_keywords || []).join("\n"))}</textarea></label>` +
        "</form>",
      onMount(body, closeFn) {
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn";
        cancel.textContent = "取消";
        cancel.addEventListener("click", closeFn);
        const save = document.createElement("button");
        save.type = "button";
        save.className = "btn primary";
        save.textContent = "保存";
        save.addEventListener("click", () => {
          k.exclude_keywords = U.linesToList(body.querySelector("#filter-exclude").value);
          k.required_keywords = U.linesToList(body.querySelector("#filter-required").value);
          closeFn();
          renderKeywordTable();
          U.toast("过滤词已更新，记得保存配置生效");
        });
        actions.appendChild(cancel);
        actions.appendChild(save);
        body.querySelector("#filter-form").appendChild(actions);
      },
    });
  }

  /** 预置排除词编辑弹窗（P2-08）：写回 State.config.preset_exclude_keywords。 */
  function editPreset() {
    const preset = State.config.preset_exclude_keywords || [];
    Modal.open({
      title: "编辑预置排除词（新关键词自动带入）",
      width: "480px",
      bodyHtml:
        '<form class="modal-form" id="preset-form">' +
        "<label>预置排除词（每行一个，保存后去重保序）" +
        `<textarea id="preset-textarea" rows="8">${U.escapeHtml(preset.join("\n"))}</textarea></label>` +
        '<p class="hint">添加新关键词时自动写入其 exclude_keywords。显式清空 = 关闭自动预置。</p>' +
        "</form>",
      onMount(body, closeFn) {
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn";
        cancel.textContent = "取消";
        cancel.addEventListener("click", closeFn);
        const save = document.createElement("button");
        save.type = "button";
        save.className = "btn primary";
        save.textContent = "保存";
        save.addEventListener("click", () => {
          const lines = U.linesToList(body.querySelector("#preset-textarea").value);
          // 去重保序（对齐 gui.resolve_preset_exclude_keywords）
          const seen = new Set();
          const unique = lines.filter((w) => {
            if (seen.has(w)) return false;
            seen.add(w);
            return true;
          });
          State.config.preset_exclude_keywords = unique;
          closeFn();
          U.toast("预置排除词已更新，保存配置后对新关键词生效");
        });
        actions.appendChild(cancel);
        actions.appendChild(save);
        body.querySelector("#preset-form").appendChild(actions);
      },
    });
  }

  // ------------------------------------------------------------------ //
  // Cookie 状态与单值保存
  // ------------------------------------------------------------------ //
  function renderCookieStatus() {
    const health = (State.config && State.config.cookie_health) || { state: "unknown", text: "未知" };
    const light = U.$("#cookie-status-light");
    if (light) {
      light.className = "status-light " + (health.state || "unknown");
      light.title = health.text || "";
    }
    const textEl = U.$("#cookie-status-text");
    if (textEl) textEl.textContent = health.text || "—";
    const maskedEl = U.$("#cookie-masked");
    if (maskedEl) {
      maskedEl.textContent = State.config && State.config.cookies_masked
        ? "（已保存：" + State.config.cookies_masked + "）"
        : "";
    }
  }

  async function saveCookie() {
    const input = U.$("#cookie-input");
    const cookie = input.value.trim();
    if (!cookie) {
      U.fieldError(input, "请先粘贴 Cookie");
      return;
    }
    const btn = U.$("#cookie-save-btn");
    U.setBtnLoading(btn, true, "校验中…");
    try {
      const result = await Api.api("/api/cookie/save", { method: "POST", body: { cookie } });
      U.toast(result.message || "Cookie 已更新并加密保存");
      input.value = "";
      await load();
    } catch (err) {
      U.toast("Cookie 保存失败：" + err.message, true);
    } finally {
      U.setBtnLoading(btn, false, "校验并保存");
    }
  }

  // ------------------------------------------------------------------ //
  // Cookie 池管理弹窗（P2-01）
  // ------------------------------------------------------------------ //
  let poolSelected = null;

  function poolHealthClass(state) {
    return state === "ok" ? "ok" : state === "expiring" ? "expiring" : "bad";
  }

  function renderPoolTable(body) {
    const tbody = body.querySelector("#pool-tbody");
    if (!tbody) return;
    const data = body._poolData || { pool: [], single: {} };
    const pool = data.pool || [];
    if (!pool.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">池为空（使用单值 Cookie）</td></tr>';
      return;
    }
    tbody.innerHTML = pool
      .map(
        (p, i) => `
      <tr data-i="${i}" data-name="${U.escapeHtml(p.name)}" class="${poolSelected === p.name ? "selected" : ""}">
        <td><input type="radio" name="pool-select" value="${U.escapeHtml(p.name)}"></td>
        <td>${U.escapeHtml(p.name)}</td>
        <td>${p.enabled ? "启用" : "停用"}</td>
        <td><span class="pool-health ${poolHealthClass(p.health_state)}">${U.escapeHtml(p.health_state)}</span>
            <span class="filter-text">${U.escapeHtml(p.health_reason || "")}</span></td>
        <td class="nowrap">${U.escapeHtml(p.expire_at || "—")}</td>
        <td class="mono">${U.escapeHtml(p.masked || "")}</td>
      </tr>`
      )
      .join("");
    const single = data.single || {};
    const singleEl = body.querySelector("#pool-single");
    if (singleEl) {
      singleEl.textContent =
        "单值：[" +
        (single.health_state || "missing") +
        "] " +
        (single.health_reason || "") +
        (single.masked ? "（" + single.masked + "）" : "");
    }
  }

  /** 打开 Cookie 池管理弹窗（对齐 GUI on_manage_cookies）。 */
  async function openCookiePool() {
    let data;
    try {
      data = await Api.api("/api/cookie/pool");
    } catch (err) {
      U.toast("读取 Cookie 池失败：" + err.message, true);
      return;
    }
    poolSelected = null;
    const m = Modal.open({
      title: "Cookie 池管理",
      width: "760px",
      bodyHtml: `
        <div class="pool-toolbar">
          <button class="btn small primary" data-pool="add" type="button">➕ 添加</button>
          <button class="btn small" data-pool="edit" type="button">✏ 编辑选中</button>
          <button class="btn small" data-pool="refresh" type="button">🔄 刷新选中</button>
          <button class="btn small danger" data-pool="delete" type="button">🗑 删除选中</button>
          <button class="btn small" data-pool="toggle" type="button">⏻ 启用/停用</button>
          <button class="btn small" data-pool="auto_disable" type="button">⏹ 自动停用过期项</button>
          <button class="btn small" data-pool="detect" type="button">🔍 检测全部</button>
          <button class="btn small" data-pool="set_default" type="button">⭐ 设为默认</button>
          <button class="btn small ghost" data-pool="help" type="button">❓ 如何获取 Cookie</button>
        </div>
        <div class="table-scroll">
          <table class="pool-table">
            <thead><tr>
              <th class="checkbox-col"></th><th>名称</th><th>状态</th>
              <th>有效性</th><th>过期时间</th><th>脱敏值</th>
            </tr></thead>
            <tbody id="pool-tbody"></tbody>
          </table>
        </div>
        <p id="pool-single" class="hint"></p>
        <p class="hint">保存后立即加密落盘（fernet1:），运行中的监测下一轮自动换用新池。</p>
      `,
      onMount(body, closeFn) {
        body._poolData = data;
        renderPoolTable(body);
        body.addEventListener("click", (e) => {
          const radio = e.target.closest('input[name="pool-select"]');
          if (radio) {
            poolSelected = radio.value;
            body.querySelectorAll("#pool-tbody tr").forEach((tr) => {
              tr.classList.toggle("selected", tr.dataset.name === radio.value);
            });
          }
          const btn = e.target.closest("button[data-pool]");
          if (!btn) return;
          poolAction(btn.dataset.pool, body, closeFn);
        });
      },
    });
  }

  function selectedPoolItem(body) {
    if (!poolSelected) return null;
    const pool = (body._poolData && body._poolData.pool) || [];
    return pool.find((p) => p.name === poolSelected) || null;
  }

  /** 池操作分发（action → POST /api/cookie/pool → 刷新表格）。 */
  async function poolAction(action, body, closeFn) {
    const Api = window.XY.Api;
    const U = window.XY.Util;
    try {
      if (action === "help") {
        Modal.open({
          title: "如何获取 Cookie",
          width: "520px",
          bodyHtml:
            '<div class="modal-message">' +
            "容器内没有浏览器，请在本机浏览器完成：<br>" +
            "1. 打开 https://www.goofish.com 并登录；<br>" +
            "2. 按 F12 打开开发者工具 → Network（网络）面板；<br>" +
            "3. 任意请求的请求头中找到 <b>Cookie:</b> 一整行，整行复制；<br>" +
            "4. 回到本页面「Cookie 管理」添加/刷新到池，或粘贴到单值输入框保存。<br><br>" +
            "必须包含 <b>_m_h5_tk=</b>（mtop 签名必需）。保存后立即加密落盘。" +
            "</div>",
        });
        return;
      }
      if (action === "detect") {
        const result = await Api.api("/api/cookie/pool");
        body._poolData = result;
        renderPoolTable(body);
        U.toast(result.message || "检测完成");
        return;
      }
      if (action === "add") {
        Modal.prompt({
          title: "添加 Cookie 条目",
          fields: [
            { key: "name", label: "名称（如 主账号 / 小号1）", type: "text", required: true },
            { key: "cookie", label: "Cookie 请求头（须含 _m_h5_tk=）", type: "textarea", rows: 4, required: true },
          ],
          onSave: async (values) => {
            const result = await Api.api("/api/cookie/pool", {
              method: "POST",
              body: { action: "add", name: values.name, cookie: values.cookie },
            });
            body._poolData = result;
            renderPoolTable(body);
            U.toast(result.message || "已添加");
          },
        });
        return;
      }
      if (action === "edit" || action === "refresh") {
        const item = selectedPoolItem(body);
        if (!item) {
          U.toast("请先选择一个条目", true);
          return;
        }
        const isEdit = action === "edit";
        Modal.prompt({
          title: (isEdit ? "编辑条目" : "刷新 Cookie") + "「" + item.name + "」",
          fields: [
            { key: "name", label: "名称", type: "text", value: item.name, required: true },
            {
              key: "cookie",
              label: "新 Cookie（留空仅改名称）",
              type: "textarea",
              rows: 4,
              value: "",
              required: false,
            },
          ],
          onSave: async (values) => {
            const bodyObj = { action: isEdit ? "update" : "refresh_selected", name: item.name };
            if (isEdit) {
              bodyObj.new_name = values.name;
              if (values.cookie && values.cookie.trim()) bodyObj.cookie = values.cookie;
            } else {
              bodyObj.cookie = values.cookie;
            }
            const result = await Api.api("/api/cookie/pool", { method: "POST", body: bodyObj });
            body._poolData = result;
            renderPoolTable(body);
            U.toast(result.message || "已更新");
          },
        });
        return;
      }
      if (action === "delete") {
        const item = selectedPoolItem(body);
        if (!item) {
          U.toast("请先选择一个条目", true);
          return;
        }
        Modal.confirm({
          title: "删除条目",
          message: `确定删除 Cookie 条目「${item.name}」吗？此操作不可撤销。`,
          danger: true,
          onConfirm: async () => {
            const result = await Api.api("/api/cookie/pool", {
              method: "POST",
              body: { action: "delete", name: item.name },
            });
            body._poolData = result;
            renderPoolTable(body);
            U.toast(result.message || "已删除");
          },
        });
        return;
      }
      if (action === "toggle") {
        const item = selectedPoolItem(body);
        if (!item) {
          U.toast("请先选择一个条目", true);
          return;
        }
        const result = await Api.api("/api/cookie/pool", {
          method: "POST",
          body: { action: "toggle", name: item.name },
        });
        body._poolData = result;
        renderPoolTable(body);
        U.toast(result.message || "已切换");
        return;
      }
      if (action === "set_default") {
        const item = selectedPoolItem(body);
        if (!item) {
          U.toast("请先选择一个条目", true);
          return;
        }
        const result = await Api.api("/api/cookie/pool", {
          method: "POST",
          body: { action: "set_default", name: item.name },
        });
        U.toast(result.message || "已设为默认");
        return;
      }
      if (action === "auto_disable") {
        const result = await Api.api("/api/cookie/pool", {
          method: "POST",
          body: { action: "auto_disable_expired" },
        });
        body._poolData = result;
        renderPoolTable(body);
        U.toast(result.message || "已自动停用");
        return;
      }
    } catch (err) {
      // add 缺 _m_h5_tk → 弹二次确认后 force=true 重试
      if (err.message && err.message.includes("_m_h5_tk") && err.message.includes("仍要添加吗")) {
        Modal.confirm({
          title: "缺少 _m_h5_tk",
          message: err.message,
          danger: true,
          confirmText: "仍要添加",
          onConfirm: async () => {
            // 重新弹添加表单，但用 force=true
            Modal.prompt({
              title: "添加 Cookie 条目（强制）",
              fields: [
                { key: "name", label: "名称", type: "text", required: true },
                { key: "cookie", label: "Cookie 请求头", type: "textarea", rows: 4, required: true },
              ],
              onSave: async (values) => {
                const result = await Api.api("/api/cookie/pool", {
                  method: "POST",
                  body: {
                    action: "add",
                    name: values.name,
                    cookie: values.cookie,
                    force_missing_token: true,
                  },
                });
                body._poolData = result;
                renderPoolTable(body);
                U.toast(result.message || "已添加");
              },
            });
          },
        });
        return;
      }
      U.toast("操作失败：" + (err.message || ""), true);
    }
  }

  // ------------------------------------------------------------------ //
  // 表单收集与保存
  // ------------------------------------------------------------------ //
  function collectForm() {
    const config = State.config;
    config.interval_seconds = parseInt(U.$("#interval-input").value, 10) || 600;
    config.pages = parseInt(U.$("#pages-input").value, 10) || 1;
    config.page_size = parseInt(U.$("#page-size-input").value, 10) || 30;
    config.page_sleep = parseFloat(U.$("#page-sleep-input").value) || 0;
    config.user_agent = U.$("#user-agent-input").value.trim();
    config.fetcher_type = U.$("#fetcher-select").value;
    config.cookie_alert_enabled = U.$("#cookie-alert-input").checked;
    config.cookie_check_interval_seconds =
      parseInt(U.$("#cookie-check-interval-input").value, 10) || 0;
    return config;
  }

  async function saveConfig() {
    const statusEl = U.$("#config-save-status");
    const btn = U.$("#config-save-btn");
    statusEl.textContent = "保存中…";
    U.setBtnLoading(btn, true, "保存中…");
    try {
      const form = collectForm();
      const result = await Api.api("/api/config", { method: "PUT", body: { form } });
      statusEl.textContent = result.restarted ? "已保存并热重启 ✓" : "已保存 ✓";
      U.toast(result.message || "配置已保存并生效");
      await load();
    } catch (err) {
      statusEl.textContent = "保存失败 ✗";
      U.toast("保存失败：" + err.message, true);
    } finally {
      U.setBtnLoading(btn, false, "保存配置");
    }
  }

  /** 加载配置 → 渲染 Tab1 全部（含骨架占位替换）。 */
  async function load() {
    try {
      const data = await Api.api("/api/config");
      State.config = data;
      renderKeywordTable();
      U.$("#interval-input").value = data.interval_seconds || 600;
      U.$("#pages-input").value = data.pages || 1;
      U.$("#page-size-input").value = data.page_size != null ? data.page_size : 30;
      U.$("#page-sleep-input").value = data.page_sleep != null ? data.page_sleep : 2;
      U.$("#user-agent-input").value = data.user_agent || "";
      U.$("#fetcher-select").value = data.fetcher_type || "mtop";
      U.$("#cookie-alert-input").checked = data.cookie_alert_enabled !== false;
      U.$("#cookie-check-interval-input").value = data.cookie_check_interval_seconds || 0;
      renderCookieStatus();
      if (window.XY.TabNotify) window.XY.TabNotify.renderChannels(data);
    } catch (err) {
      U.toast("加载配置失败：" + err.message, true);
      if (window.XY.App) window.XY.App.showError(err.message);
    }
  }

  // ------------------------------------------------------------------ //
  // 初始化
  // ------------------------------------------------------------------ //
  function init() {
    U.$("#kw-add-btn").addEventListener("click", addKeyword);
    U.$("#kw-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter") addKeyword();
    });
    U.$("#preset-edit-btn").addEventListener("click", editPreset);
    U.$("#kw-tbody").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const tr = btn.closest("tr[data-idx]");
      if (!tr) return;
      const idx = Number(tr.dataset.idx);
      const action = btn.dataset.action;
      if (action === "delete") {
        State.config.keywords.splice(idx, 1);
        renderKeywordTable();
      } else if (action === "filter") {
        editFilter(idx);
      } else if (action === "edit") {
        editKeyword(idx);
      }
    });
    U.$("#kw-tbody").addEventListener("change", (e) => {
      const input = e.target.closest('input[data-action="toggle"]');
      if (!input) return;
      const tr = input.closest("tr[data-idx]");
      if (!tr) return;
      const idx = Number(tr.dataset.idx);
      State.config.keywords[idx].enabled = input.checked;
      renderKeywordTable();
    });
    U.$("#cookie-save-btn").addEventListener("click", saveCookie);
    U.$("#cookie-clear-btn").addEventListener("click", () => {
      U.$("#cookie-input").value = "";
    });
    U.$("#cookie-pool-btn").addEventListener("click", openCookiePool);
    U.$("#config-save-btn").addEventListener("click", saveConfig);
  }

  return {
    init: init,
    load: load,
    renderKeywordTable: renderKeywordTable,
    openCookiePool: openCookiePool,
  };
})();
