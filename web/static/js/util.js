/* ===========================================================
 * 闲鱼低价提醒工具 —— util.js（通用工具，无依赖）
 * 挂载到 window.XY.Util。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.Util = (function () {
  "use strict";

  /** querySelector 快捷方式。 */
  function $(sel) {
    return document.querySelector(sel);
  }

  /** querySelectorAll → 数组。 */
  function $$(sel) {
    return Array.from(document.querySelectorAll(sel));
  }

  /** HTML 转义（防 XSS；所有动态文本插入前必须调用）。 */
  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  let toastTimer = null;

  /** 全局 toast（底部浮层，3.2s 自动消失）。 */
  function toast(message, isError) {
    const el = $("#toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast show" + (isError ? " error" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  /** 过滤规则摘要（排除/必含）。 */
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

  /** 简单 debounce。 */
  function debounce(fn, delay) {
    let t;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  /** 按钮 loading 态（disabled + spinner，恢复原文案）。 */
  function setBtnLoading(btn, loading, text) {
    if (!btn) return;
    if (loading) {
      btn.dataset.originalText = btn.textContent;
      btn.disabled = true;
      btn.classList.add("loading");
      btn.textContent = text || "处理中…";
    } else {
      btn.disabled = false;
      btn.classList.remove("loading");
      btn.textContent = btn.dataset.originalText || text || "";
    }
  }

  /** 字段级错误：给 input 挂红框（2.6s 后移除）+ toast 提示。 */
  function fieldError(input, message) {
    if (input) {
      input.classList.add("field-error");
      setTimeout(() => input.classList.remove("field-error"), 2600);
    }
    if (message) toast(message, true);
  }

  /** 从多行文本解析为去空去重列表（每行一个词）。 */
  function linesToList(text) {
    return String(text || "")
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
  }

  return {
    $: $,
    $$: $$,
    escapeHtml: escapeHtml,
    toast: toast,
    filterSummary: filterSummary,
    debounce: debounce,
    setBtnLoading: setBtnLoading,
    fieldError: fieldError,
    linesToList: linesToList,
  };
})();
