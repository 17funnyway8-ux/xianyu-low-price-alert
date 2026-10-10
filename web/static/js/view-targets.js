/* ===========================================================
 * 闲鱼低价提醒工具 —— view-targets.js（② 监控配置）
 * 挂载到 window.XY.ViewTargets。
 *
 * 边界（重要）：
 *   - 关键词 / 监测参数 / 通道 → 走 PUT /api/config（表单整体提交）
 *   - Cookie                → 只走 POST /api/cookie/save（后端禁止表单携带明文）
 *   - 所有编辑先落到 st.config，再由「保存配置」一次性提交
 * =========================================================== */
window.XY = window.XY || {};
window.XY.ViewTargets = (function () {
  "use strict";
  const U = window.XY.Util;
  const S = window.XY.State;
  const UI = window.XY.UI;
  const D = window.XY.Data;
  const M = window.XY.Modal;
  const st = S.s;
  const esc = U.escapeHtml;

  /* ------------------------------------------------------------------ #
   * 关键词卡
   * ------------------------------------------------------------------ */

  function filtersSummary(k) {
    const parts = [];
    const ex = k.exclude_keywords || [];
    const rq = k.required_keywords || [];
    if (ex.length) parts.push("排除 " + ex.join("、"));
    if (rq.length) parts.push("必含 " + rq.join("、"));
    return parts.length ? parts.join(" · ") : "无过滤规则";
  }

  function kwCard(k, idx) {
    const off = k.enabled === false;
    const p = S.progressOf(k.keyword);
    let line = filtersSummary(k);
    if (p) line += " · 现价 " + U.money(p.price) + "（进度 " + U.pct(p.ratio) + "）";
    return (
      '<div class="kw-card' + (off ? " disabled" : "") + '" data-idx="' + idx + '">' +
      '<div class="kw-head">' +
      '<span class="kw-title">' + esc(k.keyword) + "</span>" +
      '<span class="badge kw">阈值 ' + esc(U.money(k.max_price)) + "</span>" +
      (off ? '<span class="badge off">已停用</span>' : "") +
      '<span class="spacer"></span>' +
      '<label class="switch" title="启用 / 停用"><input type="checkbox" data-action="kw-toggle"' +
      (off ? "" : " checked") + '><span class="track"></span></label>' +
      '<button class="btn sm" type="button" data-action="kw-edit">编辑</button>' +
      '<button class="btn sm" type="button" data-action="kw-filters">过滤词</button>' +
      '<button class="btn sm danger-ghost" type="button" data-action="kw-del">删除</button>' +
      "</div>" +
      '<div class="card-sub" style="margin-top:6px">' + esc(line) + "</div>" +
      "</div>"
    );
  }

  function renderKeywords() {
    const box = U.$("#kwList");
    if (!box) return;
    const kws = S.keywords();
    if (!kws.length) {
      box.innerHTML = UI.emptyState(true, "还没有关键词", "在上方输入关键词与价格阈值后点「添加」");
      return;
    }
    box.innerHTML = kws.map(kwCard).join("");
  }

  /* ------------------------------------------------------------------ #
   * 监测参数
   * ------------------------------------------------------------------ */

  function renderParams() {
    const c = st.config;
    if (!c) return;
    const set = (id, v) => {
      const el = U.$(id);
      if (el) el.value = v === undefined || v === null ? "" : v;
    };
    set("#intervalInput", c.interval_seconds);
    set("#fetcherSelect", c.fetcher_type || "mtop");
    set("#pagesInput", c.pages);
    set("#pageSizeInput", c.page_size);
    set("#pageSleepInput", c.page_sleep);
    set("#userAgentInput", c.user_agent);
    const alert = U.$("#cookieAlertInput");
    if (alert) alert.checked = c.cookie_alert_enabled !== false;
    set("#cookieCheckIntervalInput", c.cookie_check_interval_seconds);

    const sp = U.$("#storagePathText");
    if (sp) sp.textContent = c.storage_path || "—";
  }

  /* ------------------------------------------------------------------ #
   * Cookie 区
   * ------------------------------------------------------------------ */

  /** v1.9：渲染 Cookie 分层状态与保活（登录态 / 会话凭据 / 令牌 / 风控）。 */
  async function renderCookieLayers() {
    const box = U.$("#cookieLayers");
    if (!box) return;
    try {
      const res = await D.cookieStatus();
      const diag = res.diagnosis || {};
      const ka = res.keepalive || {};
      const rows = (diag.layers || []).map((l) => {
        const extra = l.remaining_ms != null ? "（" + U.relFuture(l.remaining_ms) + "）" : "";
        return '<div class="cl-row cl-' + esc(l.state) + '">' +
          '<span class="cl-label">' + esc(l.label) + "</span>" +
          '<span class="cl-text">' + esc(l.summary) + extra + "</span></div>";
      }).join("");
      const kaText = ka.enabled
        ? "已开启（每 " + Math.round((ka.interval_seconds || 0) / 60) + " 分钟维持一次登录态）"
        : "已关闭";
      box.innerHTML = rows +
        '<div class="cl-row cl-keepalive"><span class="cl-label">空闲保活</span>' +
        '<span class="cl-text">' + kaText + "</span></div>";
    } catch (e) {
      box.innerHTML = "";
    }
  }

  function renderCookie() {
    const c = st.config;
    if (!c) return;
    const health = S.cookieHealth();
    const meta = S.cookieMeta();

    const light = U.$("#cookieLight");
    if (light) light.className = "status-light " + meta.dot;

    const txt = U.$("#cookieStateText");
    if (txt) txt.textContent = health.text || meta.brief;

    const masked = U.$("#cookieMasked");
    if (masked) {
      let m = c.cookies_masked || "—";
      if (c.cookies_undecryptable) m += "（密文无法解密，请重新登录）";
      else if (c.cookies_was_encrypted) m += "（已加密）";
      masked.textContent = m;
    }

    renderCookieLayers();

    const def = U.$("#cookieDefault");
    if (def && st.pool) {
      def.textContent = st.pool.default_name
        ? "默认账号：" + st.pool.default_name + (st.pool.default_is_pool ? "" : "（单值）")
        : "";
    }
  }

  /* ------------------------------------------------------------------ #
   * 表单组装（字段名严格对齐 config_from_web_form）
   * ------------------------------------------------------------------ */

  function num(id, def) {
    const el = U.$(id);
    if (!el) return def;
    const v = String(el.value).trim();
    if (v === "") return def;
    return Number(v);
  }

  function buildForm() {
    const c = st.config || {};
    return {
      keywords: S.keywords().map((k) => ({
        keyword: String(k.keyword || "").trim(),
        max_price: Number(k.max_price),
        enabled: k.enabled !== false,
        exclude_keywords: (k.exclude_keywords || []).slice(),
        required_keywords: (k.required_keywords || []).slice(),
        // v1.11：规格语义过滤开关（缺省 true；只有显式关闭才写回）
        spec_filter: k.spec_filter !== false,
      })),
      interval_seconds: num("#intervalInput", 600),
      fetcher_type: (U.$("#fetcherSelect") || {}).value || "mtop",
      pages: num("#pagesInput", 1),
      page_size: num("#pageSizeInput", 30),
      page_sleep: num("#pageSleepInput", 2),
      user_agent: (U.$("#userAgentInput") || {}).value || "",
      storage_path: c.storage_path || "state/xianyu_alert.db",
      channels: c.channels || {},
      cookie_alert_enabled: !!(U.$("#cookieAlertInput") || {}).checked,
      cookie_check_interval_seconds: num("#cookieCheckIntervalInput", 0),
      preset_exclude_keywords: (c.preset_exclude_keywords || []).slice(),
    };
  }

  /** 提交前本地校验（与后端 ConfigError 的规则保持一致，避免来回一次）。 */
  function validateForm() {
    const kws = S.keywords();
    for (let i = 0; i < kws.length; i += 1) {
      const k = kws[i];
      if (!String(k.keyword || "").trim()) return "第 " + (i + 1) + " 个关键词为空";
      const p = Number(k.max_price);
      if (!isFinite(p) || p <= 0) return "关键词「" + k.keyword + "」的价格阈值必须为正数";
    }
    const interval = num("#intervalInput", 600);
    // v1.11.3：与后端 config.MIN_INTERVAL_SECONDS 保持一致的安全下限
    if (!isFinite(interval) || interval < 120) {
      return "监测间隔必须 ≥ 120 秒：频率过高会显著提高闲鱼风控（甚至封号）概率";
    }
    const pages = num("#pagesInput", 1);
    if (!isFinite(pages) || pages < 1) return "抓取页数必须 ≥ 1";
    const size = num("#pageSizeInput", 30);
    if (!isFinite(size) || size < 1 || size > 100) return "每页数量必须在 1~100 之间";
    const sleep = num("#pageSleepInput", 2);
    if (!isFinite(sleep) || sleep < 0) return "翻页间隔不能为负数";
    const ci = num("#cookieCheckIntervalInput", 0);
    if (!isFinite(ci) || ci < 0) return "Cookie 健康检测节流不能为负数";

    // 通道完整性预检（与后端 channel_is_complete 语义一致：启用即须填必填项）
    const chans = (st.config && st.config.channels) || {};
    const bad = [];
    S.CHANNEL_ORDER.forEach((ctype) => {
      const state = chans[ctype];
      if (!state || !state.enabled) return;
      const fields = S.CHANNEL_FIELDS[ctype] || [];
      const opts = state.options || {};
      fields.forEach(([key, label]) => {
        if (!String(opts[key] === undefined ? "" : opts[key]).trim()) {
          bad.push(S.CHANNEL_LABELS[ctype].split("（")[0] + " 的「" + label + "」");
        }
      });
    });
    if (bad.length) return "已启用的通道参数不完整：" + bad.slice(0, 3).join("、") + (bad.length > 3 ? " 等" : "");
    return null;
  }

  async function save(btn, statusEl) {
    const err = validateForm();
    if (err) {
      U.toast(err, true);
      if (statusEl) {
        statusEl.textContent = "✗ " + err;
        statusEl.className = "save-status err";
      }
      return false;
    }
    if (btn) U.setBtnLoading(btn, true);
    try {
      const res = await D.saveConfig(buildForm());
      S.clearDirty();
      // 同步基线快照：否则「丢弃未保存的改动」会把内存中的配置回退到
      // **保存前**的版本（configBase 仍停留在旧快照），与服务端实际配置不符。
      S.snapshotConfig();
      const msg = (res.message || "配置已保存") + (res.restarted ? "（监控已热重启）" : "");
      U.toast(msg, "hit");
      if (statusEl) {
        statusEl.textContent = "✓ " + msg;
        statusEl.className = "save-status";
      }
      window.dispatchEvent(new CustomEvent("xy:refresh-data"));
      return true;
    } catch (e) {
      U.toast("保存失败：" + e.message, true);
      if (statusEl) {
        statusEl.textContent = "✗ " + e.message;
        statusEl.className = "save-status err";
      }
      return false;
    } finally {
      if (btn) U.setBtnLoading(btn, false);
    }
  }

  /* ------------------------------------------------------------------ #
   * 关键词弹窗
   * ------------------------------------------------------------------ */

  function addKeyword() {
    const kwEl = U.$("#kwInput");
    const prEl = U.$("#priceInput");
    if (!kwEl || !prEl) return;
    // 配置尚未加载完成时 st.config 为 null，直接写会抛 TypeError
    if (!st.config) return U.toast("配置尚未加载完成，请稍后重试", true);
    const kw = String(kwEl.value || "").trim();
    const price = parseFloat(prEl.value);
    if (!kw) return U.fieldError(kwEl, "关键词不能为空");
    if (!isFinite(price) || price <= 0) return U.fieldError(prEl, "价格阈值必须为正数");
    if (S.keywords().some((k) => k.keyword === kw)) return U.fieldError(kwEl, "关键词已存在");

    st.config.keywords = st.config.keywords || [];
    st.config.keywords.push({
      keyword: kw,
      max_price: price,
      enabled: true,
      exclude_keywords: (st.config.preset_exclude_keywords || []).slice(),
      required_keywords: [],
    });
    kwEl.value = "";
    prEl.value = "";
    S.markDirty();
    renderKeywords();
    U.toast("已添加「" + kw + "」，记得保存配置", "info");
  }

  function editKeyword(idx) {
    const k = S.keywords()[idx];
    if (!k) return;
    M.prompt({
      title: "编辑关键词",
      width: "440px",
      fields: [
        { key: "keyword", label: "关键词", type: "text", value: k.keyword, required: true },
        { key: "max_price", label: "价格阈值（元）", type: "number", value: k.max_price, required: true },
      ],
      validate(values) {
        const kw = String(values.keyword || "").trim();
        const p = parseFloat(values.max_price);
        if (!kw) return "关键词不能为空";
        if (!isFinite(p) || p <= 0) return "价格阈值必须为正数";
        if (kw !== k.keyword && S.keywords().some((x) => x.keyword === kw)) return "关键词已存在";
        return null;
      },
      onSave(values) {
        k.keyword = String(values.keyword || "").trim();
        k.max_price = parseFloat(values.max_price);
        S.markDirty();
        renderKeywords();
        U.toast("已修改，记得保存配置", "info");
      },
    });
  }

  function editFilters(idx) {
    const k = S.keywords()[idx];
    if (!k) return;
    M.prompt({
      title: "「" + k.keyword + "」的过滤词",
      width: "520px",
      fields: [
        {
          key: "exclude",
          label: "排除词（每行一个：标题含这些词就跳过）",
          type: "textarea",
          rows: 6,
          value: U.listToLines(k.exclude_keywords),
        },
        {
          key: "required",
          label: "必含词（每行一个：标题必须含其中之一，留空=不限）",
          type: "textarea",
          rows: 4,
          value: U.listToLines(k.required_keywords),
        },
      ],
      onSave(values) {
        k.exclude_keywords = U.linesToList(values.exclude);
        k.required_keywords = U.linesToList(values.required);
        S.markDirty();
        renderKeywords();
        U.toast("过滤词已更新，记得保存配置", "info");
      },
    });
  }

  function editPreset() {
    const cur = (st.config && st.config.preset_exclude_keywords) || [];
    M.prompt({
      title: "编辑预置排除词",
      width: "520px",
      fields: [
        {
          key: "preset",
          label: "每行一个；新增关键词时自动写入其排除词",
          type: "textarea",
          rows: 8,
          value: U.listToLines(cur),
        },
      ],
      onSave(values) {
        st.config.preset_exclude_keywords = U.linesToList(values.preset);
        S.markDirty();
        U.toast("预置排除词已更新，记得保存配置", "info");
      },
    });
  }

  /* ------------------------------------------------------------------ #
   * Cookie 池弹窗
   * ------------------------------------------------------------------ */

  function healthBadge(state) {
    const map = {
      ok: ["hit", "有效"],
      expiring: ["warn", "即将过期"],
      expired: ["err", "已过期"],
      no_token: ["err", "缺 _m_h5_tk"],
      missing: ["off", "未配置"],
      invalid_encrypt: ["err", "无法解密"],
    };
    const m = map[state] || ["off", state || "未知"];
    return '<span class="badge ' + m[0] + '">' + esc(m[1]) + "</span>";
  }

  function poolRowsHtml(pool) {
    const items = (pool && pool.pool) || [];
    if (!items.length) {
      return UI.emptyState(true, "Cookie 池为空", "添加多个账号可在过期时自动轮换");
    }
    return (
      '<div class="black-list">' +
      items
        .map((it) => {
          const def = pool.default_is_pool && pool.default_name === it.name;
          return (
            '<div class="black-item" data-name="' + esc(it.name) + '">' +
            '<div class="black-main">' +
            "<b>" + esc(it.name) + (def ? ' <span class="badge hit">默认</span>' : "") + "</b>" +
            '<p class="masked">' + esc(it.masked || "—") +
            (it.expire_at ? " · 到期 " + esc(it.expire_at) : "") +
            (it.health_reason ? " · " + esc(it.health_reason) : "") +
            "</p>" +
            "</div>" +
            healthBadge(it.health_state) +
            '<span class="badge ' + (it.enabled ? "" : "off") + '">' + (it.enabled ? "启用" : "停用") + "</span>" +
            '<span class="spacer"></span>' +
            '<button class="btn sm" type="button" data-pool="default" data-name="' + esc(it.name) + '">设为默认</button>' +
            '<button class="btn sm" type="button" data-pool="refresh" data-name="' + esc(it.name) + '">刷新 Cookie</button>' +
            '<button class="btn sm" type="button" data-pool="rename" data-name="' + esc(it.name) + '">改名</button>' +
            '<button class="btn sm" type="button" data-pool="toggle" data-name="' + esc(it.name) + '">' +
            (it.enabled ? "停用" : "启用") + "</button>" +
            '<button class="btn sm danger-ghost" type="button" data-pool="delete" data-name="' + esc(it.name) + '">删除</button>' +
            "</div>"
          );
        })
        .join("") +
      "</div>"
    );
  }

  async function openPool() {
    const handle = M.open({
      title: "Cookie 池管理",
      width: "760px",
      bodyHtml: '<div class="hint">正在读取…</div>',
    });
    async function reload() {
      try {
        const res = await D.loadPool();
        st.pool = res;
        handle.body.innerHTML =
          '<div class="card-sub" style="margin-bottom:10px">' +
          (res.pool_used ? "池中已有可用条目，轮换生效" : "池中无「启用+健康」条目 —— 当前回退使用单值 Cookie") +
          (res.message ? " · " + esc(res.message) : "") +
          "</div>" +
          poolRowsHtml(res) +
          '<div class="modal-actions" style="justify-content:flex-start">' +
          '<button class="btn primary" type="button" data-pool="add">添加 Cookie</button>' +
          '<button class="btn" type="button" data-pool="auto-disable">自动停用过期条目</button>' +
          '<span class="spacer"></span>' +
          '<button class="btn ghost" type="button" data-pool="close">关闭</button>' +
          "</div>";
        renderCookie();
      } catch (e) {
        handle.body.innerHTML = '<div class="hint">读取 Cookie 池失败：' + esc(e.message) + "</div>";
      }
    }

    {
      handle.body.addEventListener("click", async (ev) => {
        const btn = ev.target.closest("button[data-pool]");
        if (!btn) return;
        const op = btn.dataset.pool;
        const name = btn.dataset.name || "";
        try {
          if (op === "close") {
            M.close();
            return;
          } else if (op === "add") {
            M.prompt({
              title: "添加 Cookie 条目",
              width: "560px",
              fields: [
                { key: "name", label: "条目名称（如 account-a）", type: "text", required: true },
                { key: "cookie", label: "Cookie 请求头（须含 _m_h5_tk=）", type: "textarea", rows: 5, required: true },
              ],
              async onSave(values) {
                await poolCall({ action: "add", name: values.name, cookie: values.cookie }, reload, openPool);
              },
            });
          } else if (op === "refresh") {
            M.prompt({
              title: "刷新「" + name + "」的 Cookie",
              width: "560px",
              fields: [
                { key: "cookie", label: "新的 Cookie 请求头（会先校验，无效则不保存）", type: "textarea", rows: 5, required: true },
              ],
              async onSave(values) {
                await poolCall({ action: "refresh_selected", name: name, cookie: values.cookie }, reload, openPool);
              },
            });
          } else if (op === "rename") {
            M.prompt({
              title: "重命名条目",
              width: "420px",
              fields: [{ key: "new_name", label: "新名称", type: "text", value: name, required: true }],
              async onSave(values) {
                await poolCall({ action: "update", name: name, new_name: values.new_name }, reload, openPool);
              },
            });
          } else if (op === "default") {
            await poolCall({ action: "set_default", name: name }, reload, openPool);
          } else if (op === "toggle") {
            await poolCall({ action: "toggle", name: name }, reload, openPool);
          } else if (op === "auto-disable") {
            await poolCall({ action: "auto_disable_expired" }, reload, openPool);
          } else if (op === "delete") {
            M.confirm({
              title: "删除条目",
              message: "确定删除 Cookie 条目「" + name + "」？该操作会立即写盘。",
              danger: true,
              confirmText: "删除",
              async onConfirm() {
                await poolCall({ action: "delete", name: name }, reload, openPool);
              },
            });
          }
        } catch (e) {
          U.toast(e.message, true);
        }
      });
    }

    await reload();
  }

  async function poolCall(body, reloadFn, reopenFn) {
    try {
      const res = await D.poolAction(body);
      U.toast(res.message || "操作成功", "hit");
      if (res.pool) {
        st.pool = Object.assign({}, st.pool, { pool: res.pool });
        // 池变更后重开弹窗以拿到最新 default_name / pool_used
        M.close();
        await reopenFn();
      } else if (reloadFn) {
        await reloadFn();
      }
      // 池操作可能改写了单值 Cookie（set_default）→ 需要重拉配置；
      // 但若用户有未保存的关键词/参数编辑，就不能覆盖，退化为只刷运行数据。
      window.dispatchEvent(
        new CustomEvent(st.dirty ? "xy:refresh-data" : "xy:refresh-config")
      );
    } catch (e) {
      // force_missing_token 二次确认（后端明确提示缺 _m_h5_tk）
      if (/缺少 _m_h5_tk/.test(e.message || "")) {
        const ok = window.confirm(
          "该 Cookie 缺少 _m_h5_tk，mtop 抓取很可能失败。\n\n仍要添加吗？"
        );
        if (ok) {
          await poolCall(Object.assign({}, body, { force_missing_token: true }), reloadFn, reopenFn);
        }
        return;
      }
      throw e;
    }
  }

  /* ------------------------------------------------------------------ #
   * Cookie 保存
   * ------------------------------------------------------------------ */

  async function saveCookie(btn) {
    const input = U.$("#cookieInput");
    const cookie = String(input.value || "").trim();
    if (!cookie) return U.fieldError(input, "请先粘贴 Cookie");
    U.setBtnLoading(btn, true);
    try {
      const res = await D.saveCookie(cookie);
      input.value = "";
      U.toast(res.message || "Cookie 已保存", "hit");
      window.dispatchEvent(new CustomEvent("xy:refresh-config"));
    } catch (e) {
      U.toast("Cookie 保存失败：" + e.message, true);
    } finally {
      U.setBtnLoading(btn, false);
    }
  }

  /* ---------------- 免扫码静默刷新 ---------------- */

  /**
   * 用服务端的浏览器持久化 profile 免扫码换新登录令牌。
   *
   * 前置条件（弹窗里如实告知，避免用户以为"点了没用"）：
   *   1) 服务端环境装了 Playwright；2) 此前人工登录过一次以生成 profile。
   * 任一不满足时后端会返回带替代方案的 400，前端原样展示即可。
   * 刷新成功后派发 xy:refresh-config，Cookie 状态灯会自动更新。
   */
  function refreshCookie(btn) {
    M.confirm({
      title: "静默刷新登录 Cookie",
      message:
        "将使用服务端的浏览器 profile 免扫码换取新的登录令牌（约需数秒）。\n\n" +
        "前提：服务端已安装 Playwright，且此前人工登录过一次（profile 已建立）。" +
        "若未满足，会提示改用粘贴方式。",
      confirmText: "开始刷新",
      onConfirm() {
        (async () => {
          if (btn) U.setBtnLoading(btn, true, "刷新中…");
          try {
            const res = await D.refreshCookie();
            U.toast(res.message || "Cookie 已刷新", "hit");
            window.dispatchEvent(new CustomEvent("xy:refresh-config"));
          } catch (e) {
            U.toast("刷新失败：" + e.message, true);
          } finally {
            if (btn) U.setBtnLoading(btn, false);
          }
        })();
      },
    });
  }

  /* ------------------------------------------------------------------ #
   * 渲染 / 事件
   * ------------------------------------------------------------------ */

  function render(syncForm) {
    if (syncForm) renderParams();
    renderKeywords();
    renderCookie();
  }

  function init() {
    const kwList = U.$("#kwList");
    if (kwList) {
      kwList.addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-action]");
        if (!btn) return;
        const idx = Number(btn.closest(".kw-card").dataset.idx);
        const act = btn.dataset.action;
        if (act === "kw-edit") editKeyword(idx);
        else if (act === "kw-filters") editFilters(idx);
        else if (act === "kw-del") {
          const k = S.keywords()[idx];
          // 列表可能已被一次配置刷新重排（此时 idx 越界），必须先判空
          if (!k) return U.toast("关键词列表已变更，请重试", true);
          M.confirm({
            title: "删除关键词",
            message: "确定删除「" + k.keyword + "」？已产生的提醒记录不会被删除。",
            danger: true,
            confirmText: "删除",
            onConfirm() {
              // 按**关键词名**定位删除，而不是沿用下标：确认弹窗停留期间
              // 列表可能已重排，用旧 idx 做 splice 会删掉错误的条目。
              const list = (st.config && st.config.keywords) || [];
              const at = list.findIndex((x) => x && x.keyword === k.keyword);
              if (at >= 0) list.splice(at, 1);
              S.markDirty();
              renderKeywords();
              U.toast("已删除，记得保存配置", "info");
            },
          });
        }
      });

      kwList.addEventListener("change", (e) => {
        const input = e.target.closest('input[data-action="kw-toggle"]');
        if (!input) return;
        const idx = Number(input.closest(".kw-card").dataset.idx);
        const k = S.keywords()[idx];
        k.enabled = input.checked;
        S.markDirty();
        renderKeywords();
      });
    }

    const bind = (sel, fn) => {
      const el = U.$(sel);
      if (el) el.addEventListener("click", fn);
    };
    bind("#kwAddBtn", addKeyword);
    bind("#presetEditBtn", editPreset);
    bind("#cookieRefreshBtn", (e) => refreshCookie(e.currentTarget));
    bind("#cookiePoolBtn", openPool);
    bind("#cookieClearBtn", () => {
      const i = U.$("#cookieInput");
      if (i) i.value = "";
    });
    bind("#cookieSaveBtn", (e) => saveCookie(e.currentTarget));
    bind("#configSaveBtn", (e) => save(e.currentTarget, U.$("#configSaveStatus")));

    // 参数改动 → 脏标记
    ["#intervalInput", "#fetcherSelect", "#pagesInput", "#pageSizeInput", "#pageSleepInput",
      "#userAgentInput", "#cookieAlertInput", "#cookieCheckIntervalInput"].forEach((sel) => {
      const el = U.$(sel);
      if (!el) return;
      el.addEventListener("change", () => S.markDirty());
      el.addEventListener("input", () => S.markDirty());
    });
  }

  return { init: init, render: render, save: save, buildForm: buildForm };
})();
