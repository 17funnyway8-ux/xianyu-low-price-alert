/* ===========================================================
 * 闲鱼低价提醒工具 —— app.js（入口：编排加载、轮询、事件总线）
 *
 * 架构：无构建链，多 <script> 顺序加载 + 全局命名空间 window.XY。
 * 脚本顺序（index.html）：util → api → modal → auth → state → data → ui
 *   → view-overview → view-targets → view-hits → view-system → cmdk → app
 *
 * 事件总线（window CustomEvent）：
 *   xy:refresh-config     重新拉取 /api/config（保存后、Cookie 变更后）
 *   xy:refresh-data       重新拉取 status / records / blacklist / pool
 *   xy:refresh-status     仅重新拉取 /api/monitor/status
 *   xy:refresh-hits       仅重新拉取 /api/records
 *   xy:refresh-blacklist  仅重新拉取 /api/blacklist
 *   xy:dirty              配置脏状态变化
 *   xy:view               视图切换
 *   xy:about              打开关于弹窗
 * =========================================================== */
window.XY = window.XY || {};
window.XY.App = (function () {
  "use strict";
  const U = window.XY.Util;
  const S = window.XY.State;
  const UI = window.XY.UI;
  const D = window.XY.Data;
  const M = window.XY.Modal;
  const VOverview = window.XY.ViewOverview;
  const VTargets = window.XY.ViewTargets;
  const VHits = window.XY.ViewHits;
  const VSystem = window.XY.ViewSystem;
  const Cmdk = window.XY.Cmdk;
  const st = S.s;

  const STATUS_POLL_MS = 5000;
  const DATA_POLL_MS = 30000;

  let statusTimer = null;
  let dataTimer = null;
  let countdownTimer = null;
  let loadingConfig = false;

  /* ------------------------------------------------------------------ #
   * 加载
   * ------------------------------------------------------------------ */

  async function loadHealth() {
    try {
      st.health = await D.health();
    } catch (e) {
      /* 健康检查失败不阻断 */
    }
  }

  /**
   * 拉取配置。
   * @param {boolean} force true=即使有未保存改动也覆盖（保存成功后）
   */
  async function loadConfig(force) {
    if (loadingConfig) return;
    if (!force && st.dirty) return;
    loadingConfig = true;
    try {
      const res = await D.loadConfig();
      st.config = res;
      S.snapshotConfig();
      S.clearDirty();
      VTargets.render(true);
      VSystem.render();
      UI.renderTopbar();
      UI.hideError();
    } finally {
      loadingConfig = false;
    }
  }

  async function loadStatus() {
    st.status = await D.status();
    if (st.status && st.status.running) {
      st.countdown = st.status.next_round_in;
    } else {
      st.countdown = null;
    }
    UI.renderTopbar();
    VOverview.renderRunStrip();
    VSystem.render();
  }

  async function loadRecords() {
    const res = await D.loadRecords({
      includeSold: st.filters.includeSold,
      limit: 200,
      sort: st.filters.sort,
      order: st.filters.order,
    });
    const rows = res.records || [];
    st.records = rows;
    st.recordsTotal = res.total || rows.length;

    // 筛选口径变化（如勾选「显示已售出/下架」）会一次性把一批记录带进视野。
    // 必须先重建基准，否则 detectNewHits 会把这批「刚被纳入视野」的记录
    // 当成新命中，弹出「发现低价 ¥xxx」的假提醒。
    const scope = st.filters.includeSold ? "with-sold" : "active-only";
    if (st.recordsScope !== scope) {
      st.recordsScope = scope;
      st.knownIds = null;
    }
    detectNewHits(rows);
  }

  async function loadBlacklist() {
    const res = await D.loadBlacklist(200);
    st.blacklist = res.items || [];
  }

  async function loadPool() {
    try {
      st.pool = await D.loadPool();
    } catch (e) {
      /* 池读取失败不阻断（可能未使用池） */
    }
  }

  /* ------------------------------------------------------------------ #
   * 新命中检测
   * ------------------------------------------------------------------ */

  function detectNewHits(rows) {
    const ids = rows.map((r) => String(r.product_id));
    if (st.knownIds === null) {
      st.knownIds = ids;
      return;
    }
    const before = {};
    st.knownIds.forEach((id) => (before[id] = 1));
    const fresh = rows.filter((r) => !before[String(r.product_id)]);
    st.knownIds = ids;
    if (!fresh.length) return;

    st.freshIds = {};
    fresh.forEach((r) => (st.freshIds[String(r.product_id)] = 1));
    const first = fresh[0];
    U.toast(
      "发现低价 " + U.money(first.price) + (fresh.length > 1 ? " 等 " + fresh.length + " 件" : ""),
      "hit",
      first.title
    );
    setTimeout(() => {
      st.freshIds = {};
    }, 1200);
  }

  /* ------------------------------------------------------------------ #
   * 汇总刷新
   * ------------------------------------------------------------------ */

  async function refreshData() {
    const tasks = [loadStatus(), loadRecords(), loadBlacklist(), loadPool()];
    const rs = await Promise.allSettled(tasks);
    const bad = rs.filter((r) => r.status === "rejected");
    if (bad.length === rs.length) {
      UI.showError(bad[0].reason ? bad[0].reason.message : "加载失败");
      return;
    }
    VOverview.render();
    VHits.render();
    VSystem.render();
  }

  async function refreshRecordsOnly() {
    await loadRecords();
    VOverview.render();
    VHits.render();
  }

  /* ------------------------------------------------------------------ #
   * 轮询 / 倒计时
   * ------------------------------------------------------------------ */

  function startPolling() {
    if (statusTimer) clearInterval(statusTimer);
    statusTimer = setInterval(() => {
      loadStatus().catch(() => {});
    }, STATUS_POLL_MS);

    if (dataTimer) clearInterval(dataTimer);
    dataTimer = setInterval(() => {
      refreshData().catch(() => {});
    }, DATA_POLL_MS);

    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = setInterval(() => {
      if (st.countdown === null || st.countdown === undefined) return;
      st.countdown = Math.max(0, st.countdown - 1);
      VOverview.renderRunStrip();
    }, 1000);
  }

  /* ------------------------------------------------------------------ #
   * 关于
   * ------------------------------------------------------------------ */

  async function showAbout() {
    let version = "—";
    let dataDir = "—";
    let cfgPath = "—";
    const hz = st.health || {};
    if (hz.version) version = "v" + hz.version;
    if (hz.data_dir) dataDir = hz.data_dir;
    if (st.config && st.config.storage_path) cfgPath = st.config.storage_path;
    M.open({
      title: "关于 闲鱼低价提醒工具",
      width: "540px",
      bodyHtml:
        '<div class="modal-message">' +
        "<p><b>版本</b>：" + UI.esc(version) + "</p>" +
        "<p><b>数据目录</b>：" + UI.esc(dataDir) + "</p>" +
        "<p><b>存储路径</b>：" + UI.esc(cfgPath) + "</p>" +
        "<p><b>抓取方式</b>：" + UI.esc((st.config && st.config.fetcher_type) || "—") + "</p>" +
        "<hr>" +
        "<p class='hint'>免责声明：本工具仅供个人学习与辅助使用，请遵守闲鱼平台规则，" +
        "控制抓取频率，勿用于商业用途。Cookie 仅加密保存在本机/本卷，请勿泄露。</p>" +
        "</div>",
    });
  }

  /* ------------------------------------------------------------------ #
   * 初始化
   * ------------------------------------------------------------------ */

  function bindGlobalEvents() {
    VOverview.init();
    VTargets.init();
    VHits.init();
    VSystem.init();
    Cmdk.init();
    if (window.XY.Auth) window.XY.Auth.init();

    // 导航
    U.$$(".tab").forEach((t) => {
      t.addEventListener("click", () => UI.navigate(t.dataset.view));
    });
    window.addEventListener("hashchange", () => UI.setView(UI.initialView(), { silent: true }));

    // 关于
    ["#aboutBtn", "#footerAboutBtn"].forEach((sel) => {
      const el = U.$(sel);
      if (el) el.addEventListener("click", showAbout);
    });

    // 错误条重试
    const retry = U.$("#error-retry-btn");
    if (retry) {
      retry.addEventListener("click", () => {
        UI.hideError();
        boot().catch((e) => UI.showError(e.message));
      });
    }

    // 脏条
    const saveBtn = U.$("#dirtySaveBtn");
    if (saveBtn) saveBtn.addEventListener("click", () => VTargets.save(saveBtn, U.$("#configSaveStatus")));
    const discard = U.$("#dirtyDiscardBtn");
    if (discard) {
      discard.addEventListener("click", () => {
        M.confirm({
          title: "丢弃未保存的改动",
          message: "将把关键词 / 参数 / 通道恢复为服务端当前配置，未保存的编辑会丢失。",
          danger: true,
          confirmText: "丢弃",
          onConfirm() {
            S.restoreConfig();
            VTargets.render(true);
            VSystem.render();
            U.toast("已恢复为服务端配置", "info");
          },
        });
      });
    }

    // 事件总线
    window.addEventListener("xy:dirty", UI.renderDirty);
    window.addEventListener("xy:view", (e) => {
      const v = e.detail.view;
      if (v === "system") VSystem.connectStream();
      if (v === "hits") VHits.refresh().catch((err) => U.toast(err.message, true));
    });
    window.addEventListener("xy:refresh-config", () => {
      loadConfig(true)
        .then(refreshData)
        .catch((e) => UI.showError(e.message));
    });
    window.addEventListener("xy:refresh-data", () => {
      refreshData().catch((e) => UI.showError(e.message));
    });
    window.addEventListener("xy:refresh-status", () => {
      loadStatus().catch((e) => U.toast(e.message, true));
    });
    window.addEventListener("xy:refresh-hits", () => {
      refreshRecordsOnly().catch((e) => U.toast(e.message, true));
    });
    window.addEventListener("xy:refresh-blacklist", () => {
      loadBlacklist().then(() => VSystem.render()).catch(() => {});
    });
    window.addEventListener("xy:about", showAbout);

    // 认证成功后重连 SSE
    window.addEventListener("xy:authed", () => {
      VSystem.connectStream();
      refreshData().catch(() => {});
    });
  }

  async function boot() {
    await loadHealth();
    await loadConfig(true);
    await loadBlacklist();
    await loadPool();
    await loadStatus();
    await loadRecords();

    VOverview.render();
    VHits.render();
    VSystem.render();
    UI.renderTopbar();
    UI.renderDirty();
    UI.hideError();
  }

  async function init() {
    UI.setView(UI.initialView(), { silent: true });
    bindGlobalEvents();
    // 先渲染一次骨架，避免首屏空白
    VTargets.render(false);
    VHits.render();
    VSystem.render();
    UI.renderTopbar();

    try {
      await boot();
    } catch (e) {
      UI.showError(e.message || "加载失败");
    }

    startPolling();
    // SSE 日志流常驻：切到「通知与系统」时日志已经是热的
    VSystem.connectStream();
  }

  return { init: init, boot: boot, showAbout: showAbout, refreshData: refreshData };
})();

document.addEventListener("DOMContentLoaded", function () {
  window.XY.App.init();
});
