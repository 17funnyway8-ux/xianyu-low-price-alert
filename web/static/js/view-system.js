/* ===========================================================
 * 闲鱼低价提醒工具 —— view-system.js（④ 通知与系统）
 * 挂载到 window.XY.ViewSystem。
 *
 * 内容：通知通道（6 类，写入 st.config.channels） / 实时日志（SSE） /
 *       黑名单 / 系统信息
 * =========================================================== */
window.XY = window.XY || {};
window.XY.ViewSystem = (function () {
  "use strict";
  const U = window.XY.Util;
  const S = window.XY.State;
  const UI = window.XY.UI;
  const D = window.XY.Data;
  const st = S.s;
  const esc = U.escapeHtml;

  const LOG_MAX = 600;
  let stream = null;

  /* ------------------------------------------------------------------ #
   * 通知通道
   * ------------------------------------------------------------------ */

  function channelCard(ctype) {
    const ch = (st.config && st.config.channels && st.config.channels[ctype]) || {
      enabled: false,
      options: {},
    };
    const options = ch.options || {};
    const fields = S.CHANNEL_FIELDS[ctype] || [];
    const fieldHtml = fields
      .map(([key, label, isSecret, def]) => {
        const v = options[key] !== undefined && options[key] !== null ? options[key] : def;
        return (
          "<label>" + esc(label) +
          '<input type="' + (isSecret ? "password" : "text") +
          '" data-ctype="' + esc(ctype) + '" data-field="' + esc(key) +
          '" value="' + esc(v) + '" autocomplete="off"></label>'
        );
      })
      .join("");
    const noFields =
      fields.length === 0
        ? '<div class="card-sub" style="margin-top:8px">该通道无需参数，命中会直接打印到日志区。</div>'
        : "";

    return (
      '<div class="channel' + (ch.enabled ? " enabled" : "") + '" data-ctype="' + esc(ctype) + '">' +
      '<div class="ch-head">' +
      '<span class="ch-name">' + esc(S.CHANNEL_LABELS[ctype] || ctype) + "</span>" +
      '<span class="ch-tag">' + esc(S.CHANNEL_TAGS[ctype] || "") + "</span>" +
      '<span class="spacer"></span>' +
      '<label class="switch" title="启用 / 停用"><input type="checkbox" data-ctype="' + esc(ctype) +
      '" data-action="channel-toggle"' + (ch.enabled ? " checked" : "") + '><span class="track"></span></label>' +
      "</div>" +
      (fields.length ? '<div class="ch-fields">' + fieldHtml + "</div>" : noFields) +
      '<div class="ch-actions">' +
      '<button class="btn sm" type="button" data-ctype="' + esc(ctype) + '" data-action="channel-test">测试发送</button>' +
      '<span class="ch-result" data-ctype="' + esc(ctype) + '"></span>' +
      "</div>" +
      "</div>"
    );
  }

  function renderChannels() {
    const box = U.$("#channelList");
    if (!box) return;
    if (!st.config) {
      box.innerHTML = '<div class="hint">正在读取配置…</div>';
      return;
    }
    box.innerHTML = S.CHANNEL_ORDER.map(channelCard).join("");
    const sum = U.$("#chSummary");
    if (sum) {
      const on = S.CHANNEL_ORDER.filter(
        (c) => st.config.channels && st.config.channels[c] && st.config.channels[c].enabled
      );
      sum.textContent = on.length ? "已启用 " + on.length + " 个" : "未启用任何通道";
      sum.className = "badge" + (on.length ? " hit" : " off");
    }
  }

  function collectChannelOptions(ctype) {
    const options = {};
    U.$$('.channel[data-ctype="' + ctype + '"] input[data-field]').forEach((input) => {
      options[input.dataset.field] = input.value.trim();
    });
    return options;
  }

  function ensureChannel(ctype) {
    st.config.channels = st.config.channels || {};
    if (!st.config.channels[ctype]) st.config.channels[ctype] = { enabled: false, options: {} };
    if (!st.config.channels[ctype].options) st.config.channels[ctype].options = {};
    return st.config.channels[ctype];
  }

  async function testChannel(ctype, btn, resultEl) {
    const options = collectChannelOptions(ctype);
    U.setBtnLoading(btn, true);
    if (resultEl) {
      resultEl.textContent = "发送中…";
      resultEl.className = "ch-result";
    }
    try {
      const res = await D.testNotify(ctype, options);
      if (resultEl) {
        resultEl.textContent = "✓ " + (res.message || "已发送");
        resultEl.className = "ch-result ok";
      }
      U.toast("测试消息已发送", "hit");
    } catch (e) {
      if (resultEl) {
        resultEl.textContent = "✗ " + e.message;
        resultEl.className = "ch-result err";
      }
      U.toast("测试发送失败：" + e.message, true);
    } finally {
      U.setBtnLoading(btn, false);
    }
  }

  /* ------------------------------------------------------------------ #
   * 日志（SSE）
   * ------------------------------------------------------------------ */

  function matchLog(entry) {
    const f = st.logFilter.trim();
    if (!f) return true;
    const m = f.match(/^(debug|info|warning|warn|error|critical)\+?$/i);
    if (m) {
      const plus = f.trim().endsWith("+");
      const w = U.levelWeight(m[1]);
      const lw = U.levelWeight(entry.level);
      return plus ? lw >= w : lw === w;
    }
    const text = String(entry.text || "").toLowerCase();
    return text.indexOf(f.toLowerCase()) >= 0;
  }

  function logLineHtml(entry) {
    const lv = U.levelClass(entry.level);
    const text = String(entry.text || "");
    let cls = "log-line " + lv;
    if (/命中|低于阈值|新的低价|✅/.test(text)) cls += " hit";
    return (
      '<div class="' + cls + '">' +
      '<span class="lv">[' + esc(String(entry.level || "").slice(0, 4)) + "]</span> " +
      esc(text) +
      "</div>"
    );
  }

  function renderLogs() {
    const box = U.$("#logBox");
    if (!box) return;
    const rows = st.logs.filter(matchLog);
    if (!rows.length) {
      box.innerHTML =
        '<div class="log-empty">' +
        (st.logs.length ? "没有匹配的日志" : "日志将实时显示在这里（SSE）…") +
        "</div>";
    } else {
      const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.innerHTML = rows.map(logLineHtml).join("");
      if (atBottom) box.scrollTop = box.scrollHeight;
    }
    const cnt = U.$("#logCount");
    if (cnt) cnt.textContent = rows.length + " / " + st.logs.length + " 条";
    box.style.fontSize = st.logFontSize + "px";
  }

  function pushLog(entry) {
    st.logs.push(entry);
    if (st.logs.length > LOG_MAX) st.logs = st.logs.slice(st.logs.length - LOG_MAX);
    renderLogs();
  }

  function renderStreamStatus() {
    const el = U.$("#streamStatus");
    if (!el) return;
    const map = {
      idle: ["未连接", ""],
      connected: ["已连接", "ok"],
      reconnecting: ["重连中…", "warn"],
      error: ["连接异常", "err"],
    };
    const m = map[st.streamState] || map.idle;
    el.textContent = m[0];
    // 状态色类必须真正落到元素上：原实现固定写 "card-sub"，
    // 把 map 里的 ok/warn/err 整条丢掉，四种状态在界面上毫无区别。
    el.className = "card-sub" + (m[1] ? " " + m[1] : "");
  }

  function connectStream() {
    if (stream) return;
    stream = D.connectLogStream({
      onMessage(entry) {
        if (entry && typeof entry === "object") pushLog(entry);
      },
      onStatus(s) {
        if (s === "unauthorized") {
          // 401：本轮流已终止（api.connectStream 收到 401 后直接 return）。
          // 句柄必须归零，否则认证成功后的 xy:authed 再调 connectStream()
          // 会被开头的 `if (stream) return` 挡掉 —— 启用 XY_WEB_TOKEN 的部署
          // 会表现为「token 明明正确，日志区却永远空白」。
          stream = null;
          st.streamState = "idle";
          renderStreamStatus();
          return;
        }
        st.streamState = s === "connected" ? "connected" : s === "reconnecting" ? "reconnecting" : "idle";
        renderStreamStatus();
      },
      onError() {
        st.streamState = "error";
        renderStreamStatus();
      },
    });
  }

  /* ------------------------------------------------------------------ #
   * 黑名单
   * ------------------------------------------------------------------ */

  function renderBlacklist() {
    const box = U.$("#blackList");
    if (!box) return;
    const items = st.blacklist || [];
    const cnt = U.$("#blackCount");
    if (cnt) cnt.textContent = items.length ? items.length + " 个商品" : "";
    if (!items.length) {
      box.innerHTML = UI.emptyState(true, "黑名单为空", "在「命中战果」里把噪音商品拉黑即可");
      return;
    }
    box.innerHTML = items
      .map(
        (it) =>
          '<div class="black-item" data-id="' + esc(it.product_id) + '">' +
          '<div class="black-main">' +
          "<b>" + esc(it.product_id) + "</b>" +
          "<p>" + esc(it.keyword || "—") + " · " + esc(it.reason || "—") +
          (it.created_at ? " · " + esc(U.relTime(it.created_at)) : "") + "</p>" +
          "</div>" +
          '<span class="spacer"></span>' +
          '<button class="btn sm" type="button" data-action="black-restore">恢复提醒</button>' +
          "</div>"
      )
      .join("");
  }

  async function refreshBlacklist() {
    const res = await D.loadBlacklist(200);
    st.blacklist = res.items || [];
    renderBlacklist();
  }

  /* ------------------------------------------------------------------ #
   * 系统信息
   * ------------------------------------------------------------------ */

  /** v1.11.3：风控熔断状态文案（正常 / 冷却中 + 剩余分钟 + 累计命中）。 */
  function riskText(stt) {
    const risk = stt.risk || {};
    if (!risk.active) return "正常";
    const minutes = Math.max(1, Math.ceil(Number(risk.remaining_seconds || 0) / 60));
    return "冷却中（剩 " + minutes + " 分钟 · 命中 " + (risk.hits || 0) + " 次）";
  }

  function renderSysInfo() {
    const box = U.$("#sysInfo");
    if (!box) return;
    const hz = st.health || {};
    const stt = st.status || {};
    const c = st.config || {};
    const rows = [
      ["版本", hz.version ? "v" + hz.version : "—"],
      ["数据目录", hz.data_dir || "—"],
      ["存储路径", c.storage_path || "—"],
      ["抓取方式", c.fetcher_type || "—"],
      ["监测间隔", (stt.interval_seconds || c.interval_seconds || 0) + " 秒"],
      ["关键词数", String(stt.keyword_count === undefined ? (c.keywords || []).length : stt.keyword_count)],
      ["轮次", String(stt.round_count || 0)],
      ["累计提醒", String(stt.notified_count || 0)],
      ["最近轮次", stt.last_round_at || "—"],
      ["监控状态", stt.running ? "运行中" : "未运行"],
      ["风控熔断", riskText(stt)],
    ];
    box.innerHTML = rows
      .map(([k, v]) => '<div class="kv"><span>' + esc(k) + "</span><span>" + esc(v) + "</span></div>")
      .join("");
  }

  /* ------------------------------------------------------------------ #
   * 渲染 / 事件
   * ------------------------------------------------------------------ */

  function render() {
    renderChannels();
    renderLogs();
    renderStreamStatus();
    renderBlacklist();
    renderSysInfo();
  }

  function init() {
    const list = U.$("#channelList");
    if (list) {
      list.addEventListener("change", (e) => {
        const input = e.target.closest('input[data-action="channel-toggle"]');
        if (!input) return;
        const ctype = input.dataset.ctype;
        const ch = ensureChannel(ctype);
        ch.enabled = input.checked;
        input.closest(".channel").classList.toggle("enabled", input.checked);
        S.markDirty();
        renderChannels();
        U.toast("通道已" + (input.checked ? "启用" : "停用") + "，记得保存配置", "info");
      });

      list.addEventListener("input", (e) => {
        const input = e.target.closest("input[data-field]");
        if (!input) return;
        const ctype = input.dataset.ctype;
        ensureChannel(ctype).options = collectChannelOptions(ctype);
        S.markDirty();
      });

      list.addEventListener("click", (e) => {
        const btn = e.target.closest('button[data-action="channel-test"]');
        if (!btn) return;
        const ctype = btn.dataset.ctype;
        const resultEl = list.querySelector('.ch-result[data-ctype="' + ctype + '"]');
        testChannel(ctype, btn, resultEl);
      });
    }

    const lf = U.$("#logFilter");
    if (lf) {
      lf.addEventListener(
        "input",
        U.debounce(() => {
          st.logFilter = lf.value;
          renderLogs();
        }, 150)
      );
    }

    const bind = (sel, fn) => {
      const el = U.$(sel);
      if (el) el.addEventListener("click", fn);
    };
    bind("#logClearBtn", () => {
      st.logs = [];
      renderLogs();
    });
    bind("#logFontMinusBtn", () => {
      st.logFontSize = Math.max(9, st.logFontSize - 1);
      renderLogs();
    });
    bind("#logFontPlusBtn", () => {
      st.logFontSize = Math.min(20, st.logFontSize + 1);
      renderLogs();
    });

    const dOnly = U.$("#detailOnlyInput");
    if (dOnly) {
      dOnly.addEventListener("change", async () => {
        try {
          const res = await D.setDetailOnly(dOnly.checked);
          U.toast(res.message || "已更新", "hit");
        } catch (e) {
          dOnly.checked = !dOnly.checked;
          U.toast(e.message, true);
        }
      });
    }

    const bl = U.$("#blackList");
    if (bl) {
      bl.addEventListener("click", async (e) => {
        const btn = e.target.closest('button[data-action="black-restore"]');
        if (!btn) return;
        const id = btn.closest(".black-item").dataset.id;
        U.setBtnLoading(btn, true);
        try {
          await D.restoreBlacklist(id);
          U.toast("已恢复提醒", "hit");
          await refreshBlacklist();
        } catch (err) {
          U.toast(err.message, true);
        } finally {
          U.setBtnLoading(btn, false);
        }
      });
    }
  }

  return { init: init, render: render, connectStream: connectStream, refreshBlacklist: refreshBlacklist, pushLog: pushLog };
})();
