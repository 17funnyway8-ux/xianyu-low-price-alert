/* ===========================================================
 * 闲鱼低价提醒工具 —— app.js（入口：init + 版本/关于 + 加载错误态）
 *
 * P2 前端架构：无构建链，多 <script> 顺序加载 + 全局命名空间 window.XY。
 * 本文件收敛为入口，只做初始化编排；渲染逻辑下放各 Tab 模块。
 *
 * 脚本加载顺序（index.html）：util → api → modal → auth → state
 *   → tab-config → tab-notify → tab-run → app（本文件）
 * =========================================================== */
window.XY = window.XY || {};
window.XY.App = (function () {
  "use strict";
  const U = window.XY.Util;
  const Api = window.XY.Api;
  const Modal = window.XY.Modal;

  /** 顶部错误条：显示失败信息 + 重试（P2-13）。 */
  function showError(message) {
    const banner = U.$("#error-banner");
    const text = U.$("#error-banner-text");
    if (!banner || !text) return;
    text.textContent = message || "加载失败";
    banner.classList.remove("hidden");
  }

  function hideError() {
    const banner = U.$("#error-banner");
    if (banner) banner.classList.add("hidden");
  }

  /** 关于弹窗（P2-16）：版本 / 数据目录 / 配置路径 / 链接 / 免责声明。 */
  async function showAbout() {
    let version = "v1.8.0";
    let dataDir = "—";
    let configPath = "—";
    try {
      const hz = await Api.api("/healthz");
      if (hz && hz.version) version = "v" + hz.version;
      const cfg = await Api.api("/api/config");
      if (cfg && cfg.storage_path) configPath = cfg.storage_path;
    } catch (e) {
      /* 关于弹窗不因接口失败而阻断 */
    }
    try {
      const health = await fetch("/healthz").then((r) => r.json());
      if (health && health.data_dir) dataDir = health.data_dir;
    } catch (e) {
      /* 忽略 */
    }
    Modal.open({
      title: "关于 闲鱼低价提醒工具",
      width: "520px",
      bodyHtml:
        '<div class="modal-message">' +
        "<p><b>版本</b>：" + U.escapeHtml(version) + "</p>" +
        "<p><b>数据目录</b>：" + U.escapeHtml(dataDir) + "</p>" +
        "<p><b>配置路径</b>：" + U.escapeHtml(configPath) + "</p>" +
        "<p><b>GitHub</b>：<a href='https://github.com/' target='_blank' rel='noopener'>xianyu-low-price-alert</a>（开源仓库占位）</p>" +
        "<hr>" +
        "<p class='hint'>免责声明：本工具仅供个人学习与辅助使用，" +
        "请遵守闲鱼平台规则，控制抓取频率，勿用于商业用途。" +
        "Cookie 仅加密保存在本机/本卷，请勿泄露。</p>" +
        "</div>",
    });
  }

  /** Tab 切换。 */
  function switchTab(name) {
    U.$$(".tab").forEach((t) => {
      t.classList.toggle("active", t.dataset.tab === name);
    });
    U.$$(".tab-panel").forEach((p) => {
      p.classList.toggle("active", p.id === "panel-" + name);
    });
  }

  function initTabs() {
    U.$$(".tab").forEach((t) => {
      t.addEventListener("click", () => switchTab(t.dataset.tab));
    });
  }

  async function loadVersion() {
    try {
      const hz = await Api.api("/healthz");
      U.$("#app-version").textContent = hz && hz.version ? "v" + hz.version : "v1.8.0";
    } catch (e) {
      U.$("#app-version").textContent = "v1.8.0";
    }
  }

  /** 入口：初始化各模块 + 加载配置。 */
  async function init() {
    initTabs();
    if (window.XY.Auth) window.XY.Auth.init();
    if (window.XY.TabConfig) window.XY.TabConfig.init();
    if (window.XY.TabNotify) window.XY.TabNotify.init();
    if (window.XY.TabRun) window.XY.TabRun.init();

    U.$("#about-btn").addEventListener("click", showAbout);
    U.$("#footer-about-btn").addEventListener("click", showAbout);
    U.$("#error-retry-btn").addEventListener("click", () => {
      hideError();
      window.location.reload();
    });

    loadVersion();
    try {
      await window.XY.TabConfig.load();
      hideError();
    } catch (e) {
      showError(e.message || "加载失败");
    }
  }

  return {
    init: init,
    showError: showError,
    hideError: hideError,
    showAbout: showAbout,
  };
})();

document.addEventListener("DOMContentLoaded", window.XY.App.init);
