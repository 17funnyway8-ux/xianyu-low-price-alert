/* ===========================================================
 * 闲鱼低价提醒工具 —— view-hits.js（③ 命中战果）
 * 挂载到 window.XY.ViewHits。
 *
 * 数据来源：GET /api/records（服务端排序 sort/order + include_sold）
 * 客户端只做「搜索 / 关键词 / 仅今日」三个过滤，避免与服务端排序冲突。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.ViewHits = (function () {
  "use strict";
  const U = window.XY.Util;
  const S = window.XY.State;
  const UI = window.XY.UI;
  const D = window.XY.Data;
  const M = window.XY.Modal;
  const st = S.s;
  const esc = U.escapeHtml;

  const CHECK_SHELF_POLL_MS = 2000;

  /* ---------------- 勾选 ---------------- */

  function selectedIds() {
    return Object.keys(st.selected).filter((k) => st.selected[k]);
  }

  function syncSelCount() {
    const ids = selectedIds();
    const el = U.$("#hitsSelCount");
    if (el) el.textContent = "已选 " + ids.length;
    const all = U.$("#hitsSelectAll");
    if (all) {
      const boxes = U.$$("#hitsList input[data-select]:not(:disabled)");
      all.checked = boxes.length > 0 && ids.length >= boxes.length;
      all.indeterminate = ids.length > 0 && ids.length < boxes.length;
    }
  }

  /* ---------------- 渲染 ---------------- */

  function renderKeywordOptions() {
    const sel = U.$("#hitsKeywordFilter");
    if (!sel) return;
    const cur = st.filters.keyword;
    const kws = S.knownKeywords();
    sel.innerHTML =
      '<option value="">全部关键词</option>' +
      kws.map((k) => '<option value="' + esc(k) + '">' + esc(k) + "</option>").join("");
    sel.value = kws.indexOf(cur) >= 0 ? cur : "";
    if (sel.value !== cur) st.filters.keyword = sel.value;
  }

  function renderList() {
    const box = U.$("#hitsList");
    if (!box) return;
    const rows = S.filteredRecs();
    const sub = U.$("#hitsSub");

    if (sub) {
      const total = st.recordsTotal;
      const soldNote = st.filters.includeSold ? "（含已售出/下架）" : "";
      sub.textContent =
        "共 " + total + " 条" + soldNote +
        (rows.length !== total ? " · 已筛选出 " + rows.length + " 条" : "");
    }

    if (!rows.length) {
      const hasAny = (st.records || []).length > 0;
      box.innerHTML = UI.emptyState(
        true,
        hasAny ? "没有符合筛选条件的记录" : "还没有命中记录",
        hasAny
          ? "试试清空搜索词或勾选「显示已售出/下架」"
          : st.status && st.status.running
            ? "监控运行中，命中后会出现在这里"
            : "监控未运行 —— 先去「态势总览」开始"
      );
      syncSelCount();
      return;
    }

    box.innerHTML = rows.map((r) => UI.hitCard(r, { actions: true })).join("");
    syncSelCount();
  }

  function render() {
    renderKeywordOptions();
    syncCheckShelfAvailability();
    renderList();
  }

  /**
   * 「校验在架」可用性。
   * 后端前置条件（monitor_service.start_check_shelf）：fetcher 必须是 mtop，
   * 否则直接 400「校验在架仅支持 mtop 抓取方式」。前端据此禁用按钮，
   * 避免用户点下去只换来一个报错。
   *
   * ⚠️ #checkShelfProgress 这个位置有两个写入方：本函数（能力提示）与
   *    pollCheckShelf（进度/结果）。坑在于：若只在「不支持」时写入、恢复
   *    可用时不清空，首屏渲染（config 尚未加载 → ft=null）写下的「不支持」
   *    会一直挂在页面上，出现「按钮已启用 + 提示说不支持」的自相矛盾。
   *    因此可用时必须撤回自己写下的那句话，但不能顺手擦掉进度文本。
   */
  const UNAVAILABLE_HINT = "当前抓取方式不支持校验在架";

  function syncCheckShelfAvailability() {
    const btn = U.$("#hitsCheckShelfBtn");
    if (!btn) return;
    const ft = st.config ? st.config.fetcher_type : null;
    const okFetcher = ft === "mtop";
    btn.disabled = !okFetcher;
    btn.title = okFetcher
      ? "逐个校验选中商品是否仍在架（最多 30 个）"
      : "校验在架仅支持 mtop 抓取方式（当前：" + (ft || "配置未加载") + "）";
    const el = U.$("#checkShelfProgress");
    if (!el) return;
    if (!okFetcher) el.textContent = UNAVAILABLE_HINT;
    else if (el.textContent === UNAVAILABLE_HINT) el.textContent = "";
  }

  /* ---------------- 数据加载 ---------------- */

  async function refresh() {
    const sortEl = U.$("#hitsSort");
    if (sortEl) {
      const parts = String(sortEl.value || "time:desc").split(":");
      st.filters.sort = parts[0] || "time";
      st.filters.order = parts[1] || "desc";
    }
    const res = await D.loadRecords({
      includeSold: st.filters.includeSold,
      limit: 200,
      sort: st.filters.sort,
      order: st.filters.order,
    });
    st.records = res.records || [];
    st.recordsTotal = res.total || st.records.length;
    // 清理已不在列表中的勾选
    const alive = {};
    st.records.map((r) => String(r.product_id)).forEach((id) => {
      if (st.selected[id]) alive[id] = true;
    });
    st.selected = alive;
    render();
  }

  /* ---------------- 记录操作 ---------------- */

  function withBtn(btn, fn) {
    return async () => {
      if (btn) U.setBtnLoading(btn, true);
      try {
        await fn();
      } catch (e) {
        U.toast(e.message, true);
      } finally {
        if (btn) U.setBtnLoading(btn, false);
      }
    };
  }

  async function bulk(action, btn) {
    const ids = selectedIds();
    if (!ids.length) return U.toast("请先勾选记录", true);

    if (action === "sold") {
      await withBtn(btn, async () => {
        const rs = await Promise.allSettled(ids.map((id) => D.markSold(id)));
        const ok = rs.filter((r) => r.status === "fulfilled").length;
        const bad = rs.length - ok;
        U.toast("已标记售出 " + ok + " 条" + (bad ? "，失败 " + bad + " 条" : ""), bad ? "warn" : "hit");
        st.selected = {};
        await refresh();
      })();
    } else if (action === "black") {
      M.prompt({
        title: "批量加入黑名单（" + ids.length + " 条）",
        width: "440px",
        fields: [{ key: "reason", label: "原因（可选）", type: "text", value: "人工剔除" }],
        onSave(values) {
          withBtn(btn, async () => {
            const rs = await Promise.allSettled(ids.map((id) => D.blacklist(id, values.reason)));
            const ok = rs.filter((r) => r.status === "fulfilled").length;
            U.toast("已加入黑名单 " + ok + " 条（不再提醒）", "hit");
            st.selected = {};
            await refresh();
            window.dispatchEvent(new CustomEvent("xy:refresh-blacklist"));
          })();
        },
      });
    }
  }

  function checkShelf(btn) {
    const ft = st.config ? st.config.fetcher_type : null;
    if (ft !== "mtop") {
      return U.toast("校验在架仅支持 mtop 抓取方式（当前：" + (ft || "未知") + "）", true);
    }
    const ids = selectedIds();
    if (!ids.length) return U.toast("请先勾选要校验的商品", true);
    if (ids.length > 30) return U.toast("一次最多校验 30 个（后端限制），当前已选 " + ids.length, true);
    withBtn(btn, async () => {
      await D.checkShelf(ids);
      U.toast("已提交校验在架（" + ids.length + " 个）", "info");
      pollCheckShelf();
    })();
  }

  function shelfText(s) {
    if (!s) return "";
    if (s.running) {
      const done = (s.done || 0) + (s.sold || 0) + (s.unknown || 0);
      return "校验在架中：" + done + "/" + s.total + "（售出 " + (s.sold || 0) + "）…";
    }
    if (s.finished_at) {
      return "校验完成：共 " + s.total + "，售出 " + (s.sold || 0) + "，未知 " + (s.unknown || 0) +
        (s.cancelled ? "（已中止）" : "");
    }
    return "";
  }

  function pollCheckShelf() {
    const el = U.$("#checkShelfProgress");
    if (st.checkShelfTimer) clearInterval(st.checkShelfTimer);
    const tick = async () => {
      try {
        const res = await D.checkShelfStatus();
        st.checkShelf = res;
        if (el) el.textContent = shelfText(res);
        if (!res.running) {
          clearInterval(st.checkShelfTimer);
          st.checkShelfTimer = null;
          await refresh();
        }
      } catch (e) {
        clearInterval(st.checkShelfTimer);
        st.checkShelfTimer = null;
        if (el) el.textContent = "";
      }
    };
    tick();
    st.checkShelfTimer = setInterval(tick, CHECK_SHELF_POLL_MS);
  }

  /* ---------------- 事件 ---------------- */

  function init() {
    const list = U.$("#hitsList");
    if (list) {
      list.addEventListener("change", (e) => {
        const cb = e.target.closest("input[data-select]");
        if (!cb) return;
        st.selected[cb.value] = cb.checked;
        if (!cb.checked) delete st.selected[cb.value];
        syncSelCount();
      });

      list.addEventListener("click", async (e) => {
        const btn = e.target.closest("button[data-action]");
        if (!btn) return;
        const id = btn.closest(".hitcard").dataset.id;
        const act = btn.dataset.action;
        try {
          if (act === "sold-one") {
            await D.markSold(id);
            U.toast("已标记为已售出/下架", "hit");
            await refresh();
          } else if (act === "unmark-one") {
            await D.unmarkSold(id);
            U.toast("已恢复为在架", "hit");
            await refresh();
          } else if (act === "black-one") {
            M.prompt({
              title: "加入黑名单",
              width: "420px",
              fields: [{ key: "reason", label: "原因（可选）", type: "text", value: "人工剔除" }],
              async onSave(values) {
                await D.blacklist(id, values.reason);
                U.toast("已加入黑名单（不再提醒）", "hit");
                await refresh();
                window.dispatchEvent(new CustomEvent("xy:refresh-blacklist"));
              },
            });
          }
        } catch (err) {
          U.toast(err.message, true);
        }
      });
    }

    const search = U.$("#hitsSearch");
    if (search) {
      search.addEventListener(
        "input",
        U.debounce(() => {
          st.filters.q = search.value;
          renderList();
        }, 180)
      );
    }

    const kwSel = U.$("#hitsKeywordFilter");
    if (kwSel) {
      kwSel.addEventListener("change", () => {
        st.filters.keyword = kwSel.value;
        renderList();
      });
    }

    const sortEl = U.$("#hitsSort");
    if (sortEl) {
      sortEl.addEventListener("change", () => {
        refresh().catch((e) => U.toast(e.message, true));
      });
    }

    const today = U.$("#hitsTodayOnly");
    if (today) {
      today.checked = st.filters.todayOnly;
      today.addEventListener("change", () => {
        st.filters.todayOnly = today.checked;
        renderList();
      });
    }

    const incSold = U.$("#hitsIncludeSold");
    if (incSold) {
      incSold.checked = st.filters.includeSold;
      incSold.addEventListener("change", () => {
        st.filters.includeSold = incSold.checked;
        refresh().catch((e) => U.toast(e.message, true));
      });
    }

    const all = U.$("#hitsSelectAll");
    if (all) {
      all.addEventListener("change", () => {
        U.$$("#hitsList input[data-select]:not(:disabled)").forEach((cb) => {
          cb.checked = all.checked;
          if (all.checked) st.selected[cb.value] = true;
          else delete st.selected[cb.value];
        });
        syncSelCount();
      });
    }

    const bind = (sel, fn) => {
      const el = U.$(sel);
      if (el) el.addEventListener("click", fn);
    };
    bind("#hitsRefreshBtn", withBtn(U.$("#hitsRefreshBtn"), refresh));
    bind("#hitsBulkSoldBtn", (e) => bulk("sold", e.currentTarget));
    bind("#hitsBulkBlackBtn", (e) => bulk("black", e.currentTarget));
    bind("#hitsCheckShelfBtn", (e) => checkShelf(e.currentTarget));
    bind("#hitsClearBtn", (e) => {
      M.confirm({
        title: "清空提醒记录",
        message:
          "将清空去重记录（product + meta），黑名单会保留。\n监控运行中无法清空（后端返回 409）。",
        danger: true,
        confirmText: "清空",
        onConfirm: withBtn(e.currentTarget, async () => {
          const res = await D.clearRecords();
          U.toast(res.message || "已清空", "hit");
          st.selected = {};
          await refresh();
          window.dispatchEvent(new CustomEvent("xy:refresh-data"));
        }),
      });
    });
  }

  return { init: init, render: render, refresh: refresh, pollCheckShelf: pollCheckShelf };
})();
