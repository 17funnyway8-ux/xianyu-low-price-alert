/* ===========================================================
 * 闲鱼低价提醒工具 —— util.js（通用工具，无依赖）
 * 挂载到 window.XY.Util。
 *
 * 对外契约（供 api/auth/modal/各视图复用）：
 *   $ / $$ / escapeHtml / toast / debounce / setBtnLoading / fieldError
 *   money / pct / relTime / todayStr / datePart / levelClass / linesToList
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

  /* ---------------- Toast（多实例堆叠） ---------------- */

  const TOAST_MAX = 4;

  /**
   * 全局 toast。
   * @param {string} message 主文案
   * @param {string|boolean} [kind] true=err / "hit" / "warn" / "err" / "info"
   * @param {string} [sub] 次行小字（如商品标题）
   */
  function toast(message, kind, sub) {
    const wrap = $("#toast-wrap");
    if (!wrap) return;
    let cls = "toast";
    if (kind === true) cls += " err";
    else if (kind === "hit" || kind === "warn" || kind === "err") cls += " " + kind;
    const el = document.createElement("div");
    el.className = cls;
    const main = document.createElement("div");
    main.textContent = message;
    el.appendChild(main);
    if (sub) {
      const s = document.createElement("div");
      s.className = "t-sub";
      s.textContent = sub;
      el.appendChild(s);
    }
    wrap.appendChild(el);
    while (wrap.children.length > TOAST_MAX) wrap.removeChild(wrap.firstChild);
    const life = kind === "hit" ? 5200 : 3200;
    setTimeout(() => {
      el.style.transition = "opacity .25s";
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 260);
    }, life);
  }

  /* ---------------- 数字 / 时间格式化 ---------------- */

  /** 金额格式化：¥1,580（整数不带小数；带小数则保留两位）。 */
  function money(n) {
    const v = Number(n);
    if (!isFinite(v)) return "—";
    const hasDec = Math.abs(v % 1) > 1e-9;
    return (
      "¥" +
      v.toLocaleString("zh-CN", {
        minimumFractionDigits: hasDec ? 2 : 0,
        maximumFractionDigits: 2,
      })
    );
  }

  /** 百分比（整数）。 */
  function pct(v) {
    const n = Number(v);
    return isFinite(n) ? Math.round(n * 100) + "%" : "—";
  }

  /** 今天日期字符串 YYYY-MM-DD（本地时区）。 */
  function todayStr() {
    return datePart(new Date());
  }

  /** Date → YYYY-MM-DD（本地时区）。 */
  function datePart(d) {
    const p = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  }

  /**
   * 把后端时间字符串归一到 YYYY-MM-DD HH:MM:SS。
   * 后端 last_seen / first_seen 由 SQLite 写入，形如 "2026-09-21 17:35:12"，
   * 但也可能是 ISO "2026-09-21T17:35:12"；两种都要能吃。
   */
  function normTime(s) {
    const t = String(s == null ? "" : s).trim();
    if (!t) return "";
    return t.replace("T", " ");
  }

  /** 取日期部分（YYYY-MM-DD）；无法解析返回 ""。 */
  function dayOf(s) {
    const t = normTime(s);
    return /^\d{4}-\d{2}-\d{2}/.test(t) ? t.slice(0, 10) : "";
  }

  /**
   * 相对时间："刚刚 / 12 分钟前 / 3 小时前 / 昨天 17:35 / 09-18 17:35"。
   * 无法解析时原样返回（不编造）。
   */
  function relTime(s) {
    const t = normTime(s);
    if (!t) return "—";
    const m = t.match(/^(\d{4})-(\d{2})-(\d{2})[ ](\d{2}):(\d{2})(?::(\d{2}))?/);
    if (!m) return t;
    const d = new Date(
      Number(m[1]), Number(m[2]) - 1, Number(m[3]),
      Number(m[4]), Number(m[5]), Number(m[6] || 0)
    );
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 0) return t.slice(11, 16); // 时钟漂移：不显示负数
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + " 分钟前";
    const today = todayStr();
    const day = m[1] + "-" + m[2] + "-" + m[3];
    const hm = t.slice(11, 16);
    if (day === today) return diff < 12 * 3600 ? Math.floor(diff / 3600) + " 小时前" : hm;
    const yd = new Date();
    yd.setDate(yd.getDate() - 1);
    if (day === datePart(yd)) return "昨天 " + hm;
    return day.slice(5) + " " + hm;
  }

  /** 秒 → "3 分 12 秒" / "45 秒"。 */
  function clockText(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    if (s < 60) return s + " 秒";
    const m = Math.floor(s / 60);
    const r = s % 60;
    return r ? m + " 分 " + r + " 秒" : m + " 分";
  }

  /* ---------------- 日志 ---------------- */

  /** 后端日志级别 → CSS 类。 */
  function levelClass(level) {
    const l = String(level || "").toUpperCase();
    if (l === "ERROR" || l === "CRITICAL") return "err";
    if (l === "WARNING" || l === "WARN") return "warn";
    if (l === "INFO") return "info";
    return "";
  }

  /** 级别权重（用于 "warning+" 这类过滤）。 */
  function levelWeight(level) {
    const order = { DEBUG: 10, INFO: 20, WARNING: 30, WARN: 30, ERROR: 40, CRITICAL: 50 };
    return order[String(level || "").toUpperCase()] || 0;
  }

  /* ---------------- 交互辅助 ---------------- */

  /** 简单 debounce。 */
  function debounce(fn, delay) {
    let t;
    return function (...args) {
      clearTimeout(t);
      t = setTimeout(() => fn.apply(this, args), delay);
    };
  }

  /** 按钮 loading 态（disabled + spinner，可替换文案，结束时恢复原文案）。 */
  function setBtnLoading(btn, loading, text) {
    if (!btn) return;
    if (loading) {
      // 仅在「进入」时记一次原文案：重复调用不会把 loading 文案覆盖成原文案
      if (btn.dataset.loadingActive !== "1") {
        btn.dataset.originalText = btn.textContent;
        btn.dataset.loadingActive = "1";
      }
      if (text) btn.textContent = text;
      btn.disabled = true;
      btn.classList.add("loading");
    } else {
      btn.disabled = false;
      btn.classList.remove("loading");
      if (btn.dataset.loadingActive === "1") {
        btn.textContent = btn.dataset.originalText || "";
        delete btn.dataset.loadingActive;
      }
    }
  }

  /** 字段级错误：给 input 挂红框（2.6s 后移除）+ toast 提示。 */
  function fieldError(input, message) {
    if (input) {
      input.classList.add("field-error");
      setTimeout(() => input.classList.remove("field-error"), 2600);
      if (typeof input.focus === "function") input.focus();
    }
    if (message) toast(message, true);
  }

  /** 从多行/逗号文本解析为去空去重列表。 */
  function linesToList(text) {
    return String(text || "")
      .split(/[\n,，]/)
      .map((s) => s.trim())
      .filter(Boolean)
      .filter((v, i, arr) => arr.indexOf(v) === i);
  }

  /** 列表 → 多行文本。 */
  function listToLines(list) {
    return (list || []).join("\n");
  }

  /**
   * 在图片地址后追加阿里 CDN 的尺寸后缀（如 _200x200.jpg）。
   * 已带尺寸后缀的地址先替换旧后缀，避免叠成 xxx.jpg_200x200.jpg_400x400.jpg；
   * 非 http(s) 或非静态图扩展名的地址原样返回（加载失败由调用方回退原图）。
   */
  function imageVariant(url, suffix) {
    const text = String(url || "").trim();
    if (!text || !/^https?:\/\//i.test(text)) return text;
    const base = text.replace(/_\d+x\d+(\.[a-z]+)$/i, "$1");
    if (!/\.(jpg|jpeg|png|webp)$/i.test(base)) return text;
    return base + suffix;
  }

  /** 列表缩略图规格（约 7KB）。 */
  function imageSmall(url) {
    return imageVariant(url, "_200x200.jpg");
  }

  /** 悬停预览规格（约 48KB）。 */
  function imageLarge(url) {
    return imageVariant(url, "_800x800.jpg");
  }

  return {
    $: $,
    $$: $$,
    escapeHtml: escapeHtml,
    toast: toast,
    money: money,
    pct: pct,
    todayStr: todayStr,
    datePart: datePart,
    normTime: normTime,
    dayOf: dayOf,
    relTime: relTime,
    clockText: clockText,
    levelClass: levelClass,
    levelWeight: levelWeight,
    debounce: debounce,
    setBtnLoading: setBtnLoading,
    fieldError: fieldError,
    linesToList: linesToList,
    listToLines: listToLines,
    imageVariant: imageVariant,
    imageSmall: imageSmall,
    imageLarge: imageLarge,
  };
})();
