/* ===========================================================
 * 闲鱼低价提醒工具 —— cmdk.js（⌘K 命令面板）
 * 挂载到 window.XY.Cmdk。
 *
 * ⌘K / Ctrl+K 打开；↑↓ 选择；Enter 执行；Esc 关闭。
 * 无输入时展示固定命令；有输入时追加「记录」匹配结果。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.Cmdk = (function () {
  "use strict";
  const U = window.XY.Util;
  const S = window.XY.State;
  const UI = window.XY.UI;
  const D = window.XY.Data;
  const st = S.s;
  const esc = U.escapeHtml;

  let items = [];
  let active = 0;
  let open = false;

  /* ---------------- 命令表 ---------------- */

  function baseCommands() {
    const running = !!(st.status && st.status.running);
    return [
      {
        label: "态势总览", key: "G O", run: () => UI.navigate("overview"),
      },
      { label: "监控配置", key: "G T", run: () => UI.navigate("targets") },
      { label: "命中战果", key: "G H", run: () => UI.navigate("hits") },
      { label: "通知与系统", key: "G S", run: () => UI.navigate("system") },
      {
        label: running ? "停止监控" : "开始监控",
        key: "M",
        run: async () => {
          const res = running ? await D.stopMonitor() : await D.startMonitor();
          U.toast(res.message || "ok", "hit");
          window.dispatchEvent(new CustomEvent("xy:refresh-data"));
        },
      },
      {
        label: "立即执行一轮",
        key: "R",
        run: async () => {
          try {
            const res = await D.runOnce();
            U.toast(res.message || "单轮执行完成", res.notified ? "hit" : "info");
            window.dispatchEvent(new CustomEvent("xy:refresh-data"));
          } catch (e) {
            U.toast(e.message, true);
          }
        },
      },
      {
        label: "保存配置", key: "S",
        run: () => {
          UI.navigate("targets");
          const btn = U.$("#configSaveBtn");
          if (btn) btn.click();
        },
      },
      { label: "刷新数据", key: "F", run: () => window.dispatchEvent(new CustomEvent("xy:refresh-data")) },
      {
        label: "Cookie 池管理",
        run: async () => {
          UI.navigate("targets");
          const btn = U.$("#cookiePoolBtn");
          if (btn) btn.click();
        },
      },
      {
        label: "过滤：仅今日命中", run: () => {
          st.filters.todayOnly = true;
          const cb = U.$("#hitsTodayOnly");
          if (cb) cb.checked = true;
          UI.navigate("hits");
          window.dispatchEvent(new CustomEvent("xy:refresh-hits"));
        },
      },
      { label: "关于", run: () => window.dispatchEvent(new CustomEvent("xy:about")) },
    ];
  }

  function recordCommands(q) {
    if (!q) return [];
    const needle = q.toLowerCase();
    return S.recs()
      .filter((r) => String(r.title).toLowerCase().indexOf(needle) >= 0)
      .slice(0, 6)
      .map((r) => ({
        label: "命中：" + r.title,
        key: U.money(r.price),
        run: () => {
          st.filters.q = q;
          st.filters.keyword = "";
          const si = U.$("#hitsSearch");
          if (si) si.value = q;
          UI.navigate("hits");
          window.dispatchEvent(new CustomEvent("xy:refresh-hits"));
        },
      }));
  }

  /* ---------------- 渲染 ---------------- */

  function build(q) {
    const needle = q.toLowerCase();
    const cmds = baseCommands().filter((c) => !needle || c.label.toLowerCase().indexOf(needle) >= 0);
    return cmds.concat(recordCommands(q));
  }

  function render(q) {
    items = build(q);
    if (active >= items.length) active = Math.max(0, items.length - 1);
    const root = U.$("#cmdk-root");
    if (!root) return;
    const listHtml = items.length
      ? items
          .map(
            (it, i) =>
              '<div class="cmdk-item' + (i === active ? " active" : "") + '" data-idx="' + i + '">' +
              "<span>" + esc(it.label) + "</span>" +
              (it.key ? '<span class="k">' + esc(it.key) + "</span>" : "") +
              "</div>"
          )
          .join("")
      : '<div class="cmdk-empty">没有匹配的命令</div>';
    root.innerHTML =
      '<div class="cmdk-mask" id="cmdkMask">' +
      '<div class="cmdk">' +
      '<input type="text" id="cmdkInput" placeholder="输入命令或搜索命中记录…" autocomplete="off" value="' + esc(q) + '">' +
      '<div class="cmdk-list" id="cmdkList">' + listHtml + "</div>" +
      "</div></div>";
    const input = U.$("#cmdkInput");
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }

  async function exec(idx) {
    const it = items[idx];
    if (!it) return;
    close();
    try {
      await it.run();
    } catch (e) {
      U.toast(e.message || "执行失败", true);
    }
  }

  /* ---------------- 开关 ---------------- */

  function close() {
    open = false;
    const root = U.$("#cmdk-root");
    if (root) root.innerHTML = "";
  }

  function show() {
    open = true;
    active = 0;
    render("");
    const root = U.$("#cmdk-root");
    if (!root) return;

    const mask = U.$("#cmdkMask");
    if (mask) {
      mask.addEventListener("click", (e) => {
        if (e.target === mask) close();
      });
    }
    const list = U.$("#cmdkList");
    if (list) {
      list.addEventListener("click", (e) => {
        const el = e.target.closest(".cmdk-item");
        if (el) exec(Number(el.dataset.idx));
      });
      list.addEventListener("mousemove", (e) => {
        const el = e.target.closest(".cmdk-item");
        if (!el) return;
        const i = Number(el.dataset.idx);
        if (i !== active) {
          active = i;
          U.$$("#cmdkList .cmdk-item").forEach((n, k) => n.classList.toggle("active", k === active));
        }
      });
    }
    const input = U.$("#cmdkInput");
    if (input) {
      input.addEventListener("input", () => {
        active = 0;
        const q = input.value;
        render(q);
        // 重新渲染后焦点会被重建的节点吃掉，这里补回
        const again = U.$("#cmdkInput");
        if (again) {
          again.focus();
          again.setSelectionRange(again.value.length, again.value.length);
        }
      });
    }
  }

  function toggle() {
    if (open) close();
    else show();
  }

  function move(delta) {
    if (!open || !items.length) return;
    active = (active + delta + items.length) % items.length;
    U.$$("#cmdkList .cmdk-item").forEach((n, k) => n.classList.toggle("active", k === active));
    const el = U.$$("#cmdkList .cmdk-item")[active];
    if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
  }

  function init() {
    document.addEventListener("keydown", (e) => {
      const meta = e.metaKey || e.ctrlKey;
      if (meta && String(e.key).toLowerCase() === "k") {
        e.preventDefault();
        toggle();
        return;
      }
      if (!open) return;
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        move(1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        move(-1);
      } else if (e.key === "Enter") {
        if (e.target && e.target.id === "cmdkInput") {
          e.preventDefault();
          exec(active);
        }
      }
    });

    const btn = U.$("#cmdkBtn");
    if (btn) btn.addEventListener("click", toggle);
  }

  return { init: init, show: show, close: close, toggle: toggle };
})();
