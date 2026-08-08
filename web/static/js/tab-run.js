/* ===========================================================
 * 闲鱼低价提醒工具 —— tab-run.js（Tab3 运行监控）
 * 挂载到 window.XY.TabRun。
 *
 * 职责（P2-03/P2-04/P2-05/P2-11/P2-12/P2-18）：
 *   - 状态条轮询 + 明细日志开关（detail_only）
 *   - SSE 实时日志（fetch 流式订阅）+ 工具条（清空/过滤/字号）
 *   - 提醒记录表：排序 / include_sold / 售出灰显 + 恢复在架 / 批量勾选
 *   - 校验在架（202 异步批处理 + 进度轮询 + 中止）
 *   - 黑名单管理弹窗（查看/恢复）
 *   - 清空记录（二次确认）
 * =========================================================== */
window.XY = window.XY || {};
window.XY.TabRun = (function () {
  "use strict";
  const U = window.XY.Util;
  const Api = window.XY.Api;
  const Modal = window.XY.Modal;
  const State = window.XY.State;

  let logStream = null;

  // ------------------------------------------------------------------ //
  // 状态条 + 明细开关
  // ------------------------------------------------------------------ //
  async function pollStatus() {
    try {
      const data = await Api.api("/api/monitor/status");
      const running = data.running;
      U.$("#monitor-status-text").textContent = running ? "状态：运行中" : "状态：未运行";
      U.$("#stat-rounds").textContent = data.round_count || 0;
      U.$("#stat-notified").textContent = data.notified_count || 0;
      U.$("#stat-last-round").textContent = data.last_round_at || "—";
      U.$("#stat-next-round").textContent =
        running && data.next_round_in != null ? `${data.next_round_in}s` : "—";
      // 明细开关回显（避免与后端状态漂移）
      const detailInput = U.$("#detail-only-input");
      if (detailInput && detailInput.checked !== !!data.detail_only) {
        detailInput.checked = !!data.detail_only;
      }
    } catch (err) {
      /* 轮询失败静默，下一轮重试 */
    }
  }

  async function monitorAction(action) {
    try {
      const result = await Api.api("/api/monitor/" + action, { method: "POST" });
      U.toast(result.message || "操作成功");
      pollStatus();
    } catch (err) {
      U.toast("操作失败：" + err.message, true);
    }
  }

  async function toggleDetailOnly() {
    const enabled = U.$("#detail-only-input").checked;
    try {
      const result = await Api.api("/api/monitor/detail_only", {
        method: "POST",
        body: { enabled },
      });
      U.toast(result.message || (enabled ? "明细日志已开启" : "明细日志已关闭"));
    } catch (err) {
      U.toast("切换失败：" + err.message, true);
      pollStatus(); // 回滚为后端实际状态
    }
  }

  // ------------------------------------------------------------------ //
  // 实时日志（SSE fetch 流式）
  // ------------------------------------------------------------------ //
  function logTagForEntry(entry) {
    const level = entry.level || "INFO";
    const text = entry.text || "";
    if (text.includes("🔔") || text.includes("命中低价") || text.includes("新出现")) return "NEW_ITEM";
    if (text.includes("✅") || text.includes("本轮完成") || text.includes("已保存") || text.includes("已启动")) {
      return "SUMMARY";
    }
    if (text.includes("🚫") || text.includes("已停用")) return "DIM";
    if (text.includes("=====") || text.includes("轮监测开始")) return "ROUND";
    return level;
  }

  function matchesLogFilter(entry) {
    const filter = (State.logFilter || "").trim().toLowerCase();
    if (!filter) return true;
    const level = (entry.level || "").toLowerCase();
    const text = (entry.text || "").toLowerCase();
    // 支持 "WARNING+" 级别过滤
    const levelMatch = filter.match(/^(debug|info|warning|error|critical)\+?$/);
    if (levelMatch) {
      const order = { debug: 0, info: 1, warning: 2, error: 3, critical: 4 };
      const min = order[levelMatch[1]];
      const hasPlus = filter.endsWith("+");
      if (hasPlus && order[level] !== undefined && order[level] >= min) return true;
      if (!hasPlus && level === levelMatch[1]) return true;
      return false;
    }
    return text.includes(filter);
  }

  function appendLogLine(entry) {
    const logBox = U.$("#log-box");
    if (!logBox) return;
    logBox.querySelector(".log-empty")?.remove();
    if (!matchesLogFilter(entry)) return;
    const tag = logTagForEntry(entry);
    const div = document.createElement("div");
    div.className = "log-line " + tag;
    div.textContent = entry.text || "";
    logBox.appendChild(div);
    // 限制 DOM 行数（对齐 MAX_LOG_LINES=2000）
    while (logBox.childElementCount > 2000) {
      logBox.removeChild(logBox.firstChild);
    }
    logBox.scrollTop = logBox.scrollHeight;
  }

  function connectLogs() {
    if (logStream) logStream.stop();
    logStream = Api.connectStream("/api/logs/stream", {
      onMessage: appendLogLine,
      onStatus(status) {
        const logBox = U.$("#log-box");
        if (!logBox) return;
        if (status === "reconnecting" && !logBox.querySelector(".log-reconnecting")) {
          const div = document.createElement("div");
          div.className = "log-line DIM log-reconnecting";
          div.textContent = "（日志连接中断，正在重连…）";
          logBox.appendChild(div);
        } else if (status === "connected") {
          logBox.querySelectorAll(".log-reconnecting").forEach((el) => el.remove());
        }
      },
      onError() {
        /* 重连由 connectStream 内部指数退避处理 */
      },
    });
  }

  function clearLogs() {
    const logBox = U.$("#log-box");
    if (logBox) logBox.innerHTML = '<div class="log-empty">日志已清空（仅前端）…</div>';
  }

  function setLogFontSize(delta) {
    State.logFontSize = Math.min(16, Math.max(10, State.logFontSize + delta));
    const logBox = U.$("#log-box");
    if (logBox) logBox.style.fontSize = State.logFontSize + "px";
    try {
      localStorage.setItem("xy_log_font_size", String(State.logFontSize));
    } catch (e) {
      /* 忽略 */
    }
  }

  // ------------------------------------------------------------------ //
  // 提醒记录表
  // ------------------------------------------------------------------ //
  function selectedRecordIds() {
    return U.$$("#record-tbody input[data-select]:checked").map((i) => i.value);
  }

  function renderRecords(records) {
    const tbody = U.$("#record-tbody");
    U.$("#records-count").textContent = records.length ? `共 ${records.length} 条` : "";
    if (!records.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">暂无提醒记录</td></tr>';
      return;
    }
    tbody.innerHTML = records
      .map((r) => {
        const sold = !!r.sold_out;
        const url = r.url
          ? `<a href="${U.escapeHtml(r.url)}" target="_blank" rel="noopener">${U.escapeHtml(r.title)}</a>`
          : U.escapeHtml(r.title);
        const soldAction = sold
          ? `<button class="btn small" data-action="unmark" type="button">↩ 恢复在架</button>`
          : `<button class="btn small" data-action="sold" type="button">标记已下架</button>`;
        return `
      <tr data-id="${U.escapeHtml(r.product_id)}" class="${sold ? "row-sold" : ""}">
        <td class="checkbox-col"><input type="checkbox" data-select value="${U.escapeHtml(r.product_id)}" ${sold ? "disabled" : ""}></td>
        <td class="nowrap">${U.escapeHtml(r.time || "")}</td>
        <td>${U.escapeHtml(r.keyword || "")}</td>
        <td class="record-title">${url}</td>
        <td class="price-cell">¥ ${Number(r.price || 0).toFixed(2)}</td>
        <td>${U.escapeHtml(r.publish || "")}</td>
        <td>
          <div class="row-actions">
            ${soldAction}
            <button class="btn small danger" data-action="blacklist" type="button">黑名单</button>
          </div>
        </td>
      </tr>`;
      })
      .join("");
  }

  async function loadRecords() {
    try {
      const includeSold = U.$("#include-sold-input").checked;
      const data = await Api.api(
        `/api/records?include_sold=${includeSold}&sort=${State.recordSort}&order=${State.recordOrder}`
      );
      renderRecords(data.records || []);
    } catch (err) {
      U.toast("加载提醒记录失败：" + err.message, true);
    }
  }

  async function recordAction(path, method, successMsg) {
    try {
      await Api.api(path, { method });
      U.toast(successMsg);
      loadRecords();
    } catch (err) {
      U.toast("操作失败：" + err.message, true);
    }
  }

  async function blacklistRecord(pid) {
    Modal.prompt({
      title: "加入黑名单",
      fields: [
        { key: "reason", label: "原因", type: "text", value: "人工剔除", required: true },
      ],
      onSave: async (values) => {
        try {
          await Api.api(`/api/records/${encodeURIComponent(pid)}/blacklist`, {
            method: "POST",
            body: { reason: values.reason || "人工剔除" },
          });
          U.toast("已加入黑名单（不再提醒）");
          loadRecords();
        } catch (err) {
          U.toast("操作失败：" + err.message, true);
        }
      },
    });
  }

  // ------------------------------------------------------------------ //
  // 批量操作（P2-18）
  // ------------------------------------------------------------------ //
  async function batchMarkSold() {
    const ids = selectedRecordIds();
    if (!ids.length) {
      U.toast("请先勾选要操作的记录", true);
      return;
    }
    Modal.confirm({
      title: "批量标记售出",
      message: `确定把选中的 ${ids.length} 条记录标记为「已售出/下架」吗？`,
      danger: true,
      onConfirm: async () => {
        let okCount = 0;
        const results = await Promise.allSettled(
          ids.map((id) =>
            Api.api(`/api/records/${encodeURIComponent(id)}/sold`, { method: "POST" })
          )
        );
        results.forEach((r) => {
          if (r.status === "fulfilled") okCount += 1;
        });
        U.toast(`已标记 ${okCount} 条（失败 ${ids.length - okCount} 条）`);
        loadRecords();
      },
    });
  }

  async function batchBlacklist() {
    const ids = selectedRecordIds();
    if (!ids.length) {
      U.toast("请先勾选要操作的记录", true);
      return;
    }
    Modal.prompt({
      title: "批量加入黑名单",
      fields: [
        { key: "reason", label: "原因（应用到全部选中项）", type: "text", value: "人工剔除", required: true },
      ],
      onSave: async (values) => {
        const reason = values.reason || "人工剔除";
        let okCount = 0;
        const results = await Promise.allSettled(
          ids.map((id) =>
            Api.api(`/api/records/${encodeURIComponent(id)}/blacklist`, {
              method: "POST",
              body: { reason },
            })
          )
        );
        results.forEach((r) => {
          if (r.status === "fulfilled") okCount += 1;
        });
        U.toast(`已加入黑名单 ${okCount} 条（失败 ${ids.length - okCount} 条）`);
        loadRecords();
      },
    });
  }

  // ------------------------------------------------------------------ //
  // 校验在架（P2-03 异步批处理）
  // ------------------------------------------------------------------ //
  async function startCheckShelf() {
    const ids = selectedRecordIds();
    if (!ids.length) {
      U.toast("请先勾选要校验的记录", true);
      return;
    }
    try {
      const result = await Api.api("/api/records/check_shelf", {
        method: "POST",
        body: { product_ids: ids },
      });
      U.toast(`已开始校验 ${result.count || ids.length} 个商品的在架状态`);
      startCheckShelfPolling();
    } catch (err) {
      U.toast("校验在架失败：" + err.message, true);
    }
  }

  function startCheckShelfPolling() {
    if (State.checkShelfTimer) return;
    State.checkShelfTimer = setInterval(async () => {
      try {
        const st = await Api.api("/api/records/check_shelf/status");
        const prog = U.$("#check-shelf-progress");
        if (prog) {
          prog.textContent = st.running
            ? `校验在架：${st.done + st.sold + st.unknown}/${st.total}（售出 ${st.sold}）…`
            : `校验在架完成：共 ${st.total}，售出 ${st.sold}，未知 ${st.unknown}`;
        }
        if (!st.running) {
          clearInterval(State.checkShelfTimer);
          State.checkShelfTimer = null;
          loadRecords();
        }
      } catch (err) {
        /* 轮询失败下一轮重试 */
      }
    }, 2000);
  }

  async function cancelCheckShelf() {
    try {
      const result = await Api.api("/api/records/check_shelf/cancel", { method: "POST" });
      U.toast(result.message || "已请求中止");
    } catch (err) {
      U.toast("中止失败：" + err.message, true);
    }
  }

  // ------------------------------------------------------------------ //
  // 黑名单管理弹窗（P2-02）
  // ------------------------------------------------------------------ //
  async function openBlacklist() {
    let data;
    try {
      data = await Api.api("/api/blacklist?limit=200");
    } catch (err) {
      U.toast("读取黑名单失败：" + err.message, true);
      return;
    }
    const items = data.items || [];
    const m = Modal.open({
      title: "黑名单管理",
      width: "680px",
      bodyHtml: `
        <div class="table-scroll">
          <table class="pool-table">
            <thead><tr>
              <th class="checkbox-col"></th><th>product_id</th><th>关键词</th>
              <th>原因</th><th>加入时间</th>
            </tr></thead>
            <tbody id="bl-tbody">
              ${items.length ? "" : '<tr class="empty-row"><td colspan="5">黑名单为空</td></tr>'}
            </tbody>
          </table>
        </div>
        <p class="hint">恢复后商品可重新提醒（默认列表需重新命中低价才会出现）。</p>
      `,
      onMount(body, closeFn) {
        const tbody = body.querySelector("#bl-tbody");
        if (tbody) {
          tbody.innerHTML = items
            .map(
              (it) => `
              <tr data-pid="${U.escapeHtml(it.product_id)}">
                <td class="checkbox-col"><input type="checkbox" data-bl-select value="${U.escapeHtml(it.product_id)}"></td>
                <td class="mono">${U.escapeHtml(it.product_id)}</td>
                <td>${U.escapeHtml(it.keyword || "")}</td>
                <td>${U.escapeHtml(it.reason || "")}</td>
                <td class="nowrap">${U.escapeHtml(it.created_at || "")}</td>
              </tr>`
            )
            .join("");
        }
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const restore = document.createElement("button");
        restore.type = "button";
        restore.className = "btn primary";
        restore.textContent = "♻️ 恢复选中";
        restore.addEventListener("click", async () => {
          const pids = U.$$("#bl-tbody input[data-bl-select]:checked").map((i) => i.value);
          if (!pids.length) {
            U.toast("请先勾选要恢复的条目", true);
            return;
          }
          let okCount = 0;
          for (const pid of pids) {
            try {
              await Api.api(`/api/blacklist/${encodeURIComponent(pid)}/restore`, { method: "POST" });
              okCount += 1;
            } catch (err) {
              /* 单条失败继续 */
            }
          }
          U.toast(`已恢复 ${okCount} 条`);
          closeFn();
          loadRecords();
        });
        const closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "btn";
        closeBtn.textContent = "关闭";
        closeBtn.addEventListener("click", closeFn);
        actions.appendChild(restore);
        actions.appendChild(closeBtn);
        body.appendChild(actions);
      },
    });
  }

  // ------------------------------------------------------------------ //
  // 清空记录（P2-05 二次确认）
  // ------------------------------------------------------------------ //
  function clearRecords() {
    Modal.confirm({
      title: "清空去重记录",
      message:
        "清空后已提醒商品会重新视为新商品，可能重复提醒，不可撤销。黑名单不受影响。确定清空吗？",
      danger: true,
      confirmText: "清空",
      onConfirm: async () => {
        try {
          const result = await Api.api("/api/records/clear", { method: "POST" });
          U.toast(result.message || `已清空 ${result.deleted || 0} 条`);
          loadRecords();
        } catch (err) {
          U.toast("清空失败：" + err.message, true);
        }
      },
    });
  }

  // ------------------------------------------------------------------ //
  // 初始化
  // ------------------------------------------------------------------ //
  function init() {
    U.$("#monitor-start-btn").addEventListener("click", () => monitorAction("start"));
    U.$("#monitor-stop-btn").addEventListener("click", () => monitorAction("stop"));
    U.$("#monitor-once-btn").addEventListener("click", () => monitorAction("run_once"));
    U.$("#detail-only-input").addEventListener("change", toggleDetailOnly);
    U.$("#records-refresh-btn").addEventListener("click", loadRecords);
    U.$("#include-sold-input").addEventListener("change", loadRecords);
    U.$("#records-check-shelf-btn").addEventListener("click", startCheckShelf);
    U.$("#records-batch-sold-btn").addEventListener("click", batchMarkSold);
    U.$("#records-batch-blacklist-btn").addEventListener("click", batchBlacklist);
    U.$("#records-clear-btn").addEventListener("click", clearRecords);
    U.$("#blacklist-manage-btn").addEventListener("click", openBlacklist);
    U.$("#log-clear-btn").addEventListener("click", clearLogs);
    U.$("#log-font-plus-btn").addEventListener("click", () => setLogFontSize(1));
    U.$("#log-font-minus-btn").addEventListener("click", () => setLogFontSize(-1));
    U.$("#log-filter-input").addEventListener(
      "input",
      U.debounce(() => {
        State.logFilter = U.$("#log-filter-input").value;
        // 重放缓冲日志不可行（前端无缓存），直接清空后提示（新日志按过滤展示）
        clearLogs();
      }, 300)
    );

    U.$("#record-table").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-sort]");
      if (th) {
        const sort = th.dataset.sort;
        if (State.recordSort === sort) {
          State.recordOrder = State.recordOrder === "asc" ? "desc" : "asc";
        } else {
          State.recordSort = sort;
          State.recordOrder = "asc";
        }
        loadRecords();
        return;
      }
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const tr = btn.closest("tr[data-id]");
      if (!tr) return;
      const id = tr.dataset.id;
      if (btn.dataset.action === "sold") {
        recordAction(`/api/records/${encodeURIComponent(id)}/sold`, "POST", "已标记为已下架");
      } else if (btn.dataset.action === "unmark") {
        recordAction(`/api/records/${encodeURIComponent(id)}/unmark`, "POST", "已恢复为在架");
      } else if (btn.dataset.action === "blacklist") {
        blacklistRecord(id);
      }
    });

    // 表头全选
    U.$("#record-select-all").addEventListener("change", (e) => {
      const checked = e.target.checked;
      U.$$("#record-tbody input[data-select]:not(:disabled)").forEach((i) => {
        i.checked = checked;
      });
    });

    // 启动日志流 + 状态轮询 + 记录加载
    connectLogs();
    window.addEventListener("xy:authed", connectLogs); // 认证通过后重连 SSE
    setInterval(pollStatus, 2000);
    pollStatus();
    loadRecords();
    // 恢复记忆字号
    try {
      const saved = parseInt(localStorage.getItem("xy_log_font_size"), 10);
      if (saved >= 10 && saved <= 16) {
        State.logFontSize = saved;
        U.$("#log-box").style.fontSize = saved + "px";
      }
    } catch (e) {
      /* 忽略 */
    }
  }

  return {
    init: init,
    pollStatus: pollStatus,
    loadRecords: loadRecords,
    renderRecords: renderRecords,
    connectLogs: connectLogs,
  };
})();
