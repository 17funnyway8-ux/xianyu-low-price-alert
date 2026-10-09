/* ===========================================================
 * 闲鱼低价提醒工具 —— lightbox.js（商品大图查看）
 * 挂载到 window.XY.Lightbox。
 *
 * 三级图片规格：同一条原始 URL 的 CDN 变体，浏览器按 URL 缓存，
 * 因此"悬停看中图"不会额外下载原图。
 *     列表缩略图   _200x200.jpg   约 7KB
 *     悬停预览     _800x800.jpg   约 48KB
 *     点击查看     原图           最大细节（实测样例 1272x1696）
 *
 * 为什么不是"只做悬停放大"：
 *   - 触屏没有 hover，点击缩略图同样能看大图（同一入口）；
 *   - 小窗口悬停面板可能放不下，自动翻转到左侧并收敛进视口；
 *   - CDN 尺寸变体可能失效，逐级回退到原图，再失败就隐藏，不留破图；
 *   - 键盘可用：缩略图是 button（Enter 打开），大图支持 Esc / 左右方向键；
 *   - 一屏多条记录，大图内可左右连续翻看，不必反复回列表。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.Lightbox = (function () {
  "use strict";
  const U = window.XY.Util;

  const HOVER_DELAY_MS = 220;   // 悬停多久才弹预览，避免扫过列表时乱闪
  const EDGE = 12;              // 预览面板与视口边缘的最小间距
  const GAP = 14;               // 预览面板与缩略图之间的间距

  let previewEl = null;
  let previewImg = null;
  let previewTimer = null;

  let boxEl = null;
  let boxImg = null;
  let boxSpinner = null;
  let boxTitle = null;
  let boxMeta = null;
  let boxLink = null;
  let items = [];
  let index = 0;
  let lastFocus = null;

  /* ------------------------------------------------------------------ *
   * 悬停预览（仅精细指针设备启用）
   * ------------------------------------------------------------------ */

  function hoverCapable() {
    return !!(window.matchMedia && window.matchMedia("(hover: hover) and (pointer: fine)").matches);
  }

  function ensurePreview() {
    if (previewEl) return;
    previewEl = document.createElement("div");
    previewEl.className = "img-preview";
    previewEl.hidden = true;
    previewImg = document.createElement("img");
    previewImg.alt = "";
    previewImg.referrerPolicy = "no-referrer";
    previewImg.addEventListener("error", function () {
      // 中图变体失败 -> 回退原图；原图也失败 -> 收起面板
      const fallback = previewImg.dataset.fallback || "";
      if (fallback && previewImg.getAttribute("src") !== fallback) {
        previewImg.src = fallback;
      } else {
        hidePreview();
      }
    });
    previewEl.appendChild(previewImg);
    document.body.appendChild(previewEl);
  }

  function placePreview(btn) {
    if (!previewEl || previewEl.hidden) return;
    const rect = btn.getBoundingClientRect();
    const w = previewEl.offsetWidth || 340;
    const h = previewEl.offsetHeight || 340;
    let left = rect.right + GAP;
    if (left + w > window.innerWidth - EDGE) left = rect.left - GAP - w;  // 右侧放不下 -> 翻到左侧
    if (left < EDGE) left = EDGE;                                        // 左侧也放不下 -> 贴左边界
    let top = rect.top + rect.height / 2 - h / 2;
    top = Math.max(EDGE, Math.min(top, window.innerHeight - h - EDGE));
    previewEl.style.left = Math.round(left) + "px";
    previewEl.style.top = Math.round(top) + "px";
  }

  function showPreview(btn) {
    ensurePreview();
    const full = btn.dataset.full || "";
    if (!full) return;
    previewImg.dataset.fallback = full;
    previewImg.src = U.imageLarge(full);   // 失败时上面的 error 处理会回退原图
    previewEl.hidden = false;
    placePreview(btn);
    if (!previewImg.complete) {
      previewImg.addEventListener("load", function () {
        if (previewEl && !previewEl.hidden) placePreview(btn);
      }, { once: true });
    }
  }

  function hidePreview() {
    if (previewTimer) { clearTimeout(previewTimer); previewTimer = null; }
    if (previewEl) {
      previewEl.hidden = true;
      previewImg.removeAttribute("src");   // 停止解码，释放内存
    }
  }

  function schedulePreview(btn) {
    if (!hoverCapable()) return;
    if (previewTimer) clearTimeout(previewTimer);
    previewTimer = setTimeout(function () { showPreview(btn); }, HOVER_DELAY_MS);
  }

  /* ------------------------------------------------------------------ *
   * 大图（点击 / Enter 打开）
   * ------------------------------------------------------------------ */

  function collectItems(btn) {
    const scope = btn.closest(".hit-list") || btn.closest("#hitsList") || document;
    const nodes = scope.querySelectorAll(".hit-thumb-btn");
    const list = [];
    for (let i = 0; i < nodes.length; i += 1) {
      list.push({
        full: nodes[i].dataset.full || "",
        title: nodes[i].dataset.title || "",
        keyword: nodes[i].dataset.keyword || "",
        price: nodes[i].dataset.price || "",
        url: nodes[i].dataset.url || "",
      });
    }
    return list;
  }

  function ensureBox() {
    if (boxEl) return;
    boxEl = document.createElement("div");
    boxEl.className = "lightbox";
    boxEl.hidden = true;
    boxEl.setAttribute("role", "dialog");
    boxEl.setAttribute("aria-modal", "true");
    boxEl.setAttribute("aria-label", "商品大图");
    boxEl.innerHTML =
      '<button type="button" class="lb-close" aria-label="关闭（Esc）">×</button>' +
      '<button type="button" class="lb-nav lb-prev" aria-label="上一张（左方向键）">‹</button>' +
      '<button type="button" class="lb-nav lb-next" aria-label="下一张（右方向键）">›</button>' +
      '<div class="lb-stage"><div class="lb-spinner" hidden>加载大图…</div>' +
      '<img class="lb-img" alt="商品大图" referrerpolicy="no-referrer"></div>' +
      '<div class="lb-caption">' +
      '<div class="lb-title"></div>' +
      '<div class="lb-meta"></div>' +
      '<a class="lb-link" target="_blank" rel="noopener">在原站打开 ↗</a>' +
      '</div>';
    document.body.appendChild(boxEl);

    boxImg = boxEl.querySelector(".lb-img");
    boxSpinner = boxEl.querySelector(".lb-spinner");
    boxTitle = boxEl.querySelector(".lb-title");
    boxMeta = boxEl.querySelector(".lb-meta");
    boxLink = boxEl.querySelector(".lb-link");

    boxEl.querySelector(".lb-close").addEventListener("click", close);
    boxEl.querySelector(".lb-prev").addEventListener("click", function () { step(-1); });
    boxEl.querySelector(".lb-next").addEventListener("click", function () { step(1); });
    boxEl.addEventListener("click", function (e) {
      // 点击遮罩 / 图片区域空白处关闭（点图片本身不关）
      if (e.target === boxEl || e.target === boxEl.querySelector(".lb-stage")) close();
    });
    boxImg.addEventListener("load", function () {
      boxSpinner.hidden = true;
      boxImg.classList.add("ready");
    });
    boxImg.addEventListener("error", function () {
      boxSpinner.textContent = "大图加载失败（可能是商品已下架）";
    });
  }

  function renderBox() {
    const it = items[index] || {};
    boxTitle.textContent = it.title || "商品大图";
    const bits = [];
    if (it.keyword) bits.push(it.keyword);
    if (it.price) bits.push("¥" + it.price);
    bits.push((index + 1) + " / " + items.length);
    boxMeta.textContent = bits.join(" · ");
    if (it.url) {
      boxLink.href = it.url;
      boxLink.hidden = false;
    } else {
      boxLink.hidden = true;
    }
    const multi = items.length > 1;
    boxEl.querySelector(".lb-prev").hidden = !multi;
    boxEl.querySelector(".lb-next").hidden = !multi;

    boxImg.classList.remove("ready");
    boxImg.removeAttribute("src");
    boxSpinner.textContent = "加载大图…";
    boxSpinner.hidden = false;
    // 大图用原图，拿到最大细节
    boxImg.src = it.full || "";
  }

  function step(delta) {
    if (items.length < 2) return;
    index = (index + delta + items.length) % items.length;
    renderBox();
  }

  function open(btn) {
    ensureBox();
    hidePreview();
    items = collectItems(btn);
    index = 0;
    const target = btn.dataset.full || "";
    for (let i = 0; i < items.length; i += 1) {
      if (items[i].full === target) { index = i; break; }
    }
    lastFocus = document.activeElement;
    boxEl.hidden = false;
    document.body.classList.add("lb-open");
    renderBox();
    document.addEventListener("keydown", onKeydown);
    boxEl.querySelector(".lb-close").focus();
  }

  function close() {
    if (!boxEl || boxEl.hidden) return;
    boxEl.hidden = true;
    boxImg.removeAttribute("src");
    document.body.classList.remove("lb-open");
    document.removeEventListener("keydown", onKeydown);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
  }

  function onKeydown(e) {
    if (e.key === "Escape") { close(); }
    else if (e.key === "ArrowLeft") { step(-1); }
    else if (e.key === "ArrowRight") { step(1); }
  }

  /* ------------------------------------------------------------------ *
   * 事件绑定（委托，卡片重渲染后依然有效）
   * ------------------------------------------------------------------ */

  function init() {
    ensurePreview();
    document.addEventListener("mouseover", function (e) {
      const btn = e.target.closest ? e.target.closest(".hit-thumb-btn") : null;
      if (btn) schedulePreview(btn);
    });
    document.addEventListener("mouseout", function (e) {
      const btn = e.target.closest ? e.target.closest(".hit-thumb-btn") : null;
      if (btn) hidePreview();
    });
    document.addEventListener("click", function (e) {
      const btn = e.target.closest ? e.target.closest(".hit-thumb-btn") : null;
      if (!btn) return;
      e.preventDefault();
      open(btn);
    });
    // 滚动 / 改变窗口尺寸时，悬停预览的位置会失效，直接收起
    window.addEventListener("scroll", hidePreview, true);
    window.addEventListener("resize", hidePreview);
  }

  return { init: init, open: open, close: close };
})();
