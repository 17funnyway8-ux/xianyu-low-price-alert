/* ===========================================================
 * 闲鱼低价提醒工具 —— auth.js（认证遮罩：401 → token 输入 → 重放）
 * 挂载到 window.XY.Auth。
 *
 * 流程（对齐设计 §3.4 / P3-01）：
 *   api() 收到 401 → onUnauthorized() → showAuthOverlay()
 *   → 用户输入 token → submitToken() 试调 /api/monitor/status
 *   → 200 → 存 localStorage → 关遮罩 → 触发 xy:authed 事件（SSE 重连）
 *   → 仍 401 → 遮罩内提示「token 错误」不清除输入。
 * 不设 token 时无 401 → 遮罩永不出现（与 P1 行为一致）。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.Auth = (function () {
  "use strict";
  const U = window.XY.Util;

  function showAuthOverlay(message) {
    const overlay = U.$("#auth-overlay");
    if (!overlay) return;
    overlay.classList.remove("hidden");
    const msg = U.$("#auth-overlay-message");
    if (msg) msg.textContent = message || "该服务已开启访问认证，请输入访问 Token";
    const input = U.$("#auth-token-input");
    const err = U.$("#auth-token-error");
    if (err) err.textContent = "";
    if (input) {
      input.value = window.XY.Api.getToken();
      input.focus();
    }
  }

  function hideAuthOverlay() {
    const overlay = U.$("#auth-overlay");
    if (overlay) overlay.classList.add("hidden");
  }

  /** api() 收到 401 时调用：清 token + 弹遮罩。 */
  function onUnauthorized() {
    window.XY.Api.clearToken();
    showAuthOverlay();
  }

  /** 提交 token 校验（遮罩内按钮/回车触发）。 */
  async function submitToken() {
    const input = U.$("#auth-token-input");
    const err = U.$("#auth-token-error");
    const btn = U.$("#auth-token-btn");
    if (!input) return;
    const token = input.value.trim();
    if (!token) {
      if (err) err.textContent = "请输入 Token";
      return;
    }
    window.XY.Api.setToken(token);
    if (btn) U.setBtnLoading(btn, true, "校验中…");
    try {
      await window.XY.Api.api("/api/monitor/status");
      hideAuthOverlay();
      U.toast("认证成功");
      window.dispatchEvent(new CustomEvent("xy:authed"));
    } catch (e) {
      window.XY.Api.clearToken();
      if (err) err.textContent = "Token 错误：" + (e.message || "");
    } finally {
      if (btn) U.setBtnLoading(btn, false, "登录");
    }
  }

  function init() {
    const btn = U.$("#auth-token-btn");
    if (btn) btn.addEventListener("click", submitToken);
    const input = U.$("#auth-token-input");
    if (input) {
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") submitToken();
      });
    }
  }

  return {
    init: init,
    showAuthOverlay: showAuthOverlay,
    hideAuthOverlay: hideAuthOverlay,
    onUnauthorized: onUnauthorized,
    submitToken: submitToken,
  };
})();
