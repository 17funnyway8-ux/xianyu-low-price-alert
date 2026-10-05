/* ===========================================================
 * 闲鱼低价提醒工具 —— ui.js（共享渲染片段 + 外壳）
 * 挂载到 window.XY.UI。
 *
 * 内容：顶栏 / 错误条 / 未保存条 / 指标卡 / 命中卡 / 空状态 /
 *       视图切换 / 导航推进（hash 深链接）
 * =========================================================== */
window.XY = window.XY || {};
window.XY.UI = (function () {
  "use strict";
  const U = window.XY.Util;
  const S = window.XY.State;
  const st = S.s;

  const VIEWS = ["overview", "targets", "hits", "system"];
  const VIEW_TITLES = {
    overview: "态势总览",
    targets: "监控配置",
    hits: "命中战果",
    system: "通知与系统",
  };

  function esc(v) {
    return U.escapeHtml(v);
  }

  /* ------------------------------------------------------------------ #
   * 顶栏
   * ------------------------------------------------------------------ */

  function renderTopbar() {
    const stt = st.status || {};
    const running = !!stt.running;

    const dot = U.$("#pulseDot");
    const txt = U.$("#pulseText");
    if (dot) dot.className = "dot " + (running ? "on live" : "off");
    if (txt) txt.textContent = running ? "运行中" : "未运行";
    const pulse = U.$("#pulseBtn");
    if (pulse) pulse.title = running ? "点击停止监控" : "点击开始监控";

    // Cookie 健康灯
    const cfg = st.config;
    const health = S.cookieHealth();
    const meta = S.cookieMeta();
    const hdot = U.$("#healthDot");
    const htxt = U.$("#healthText");
    const hbtn = U.$("#healthBtn");
    if (hdot) hdot.className = "dot " + meta.dot;
    if (htxt) {
      htxt.textContent = cfg ? meta.brief : "读取中…";
      if (hbtn) hbtn.title = (cfg ? health.text || "" : "") || meta.brief;
    }

    // 命中角标
    const badge = U.$("#hitsBadge");
    if (badge) {
      const n = S.todayHits().length;
      badge.textContent = String(n);
      badge.classList.toggle("show", n > 0);
    }

    // 版本
    const ver = U.$("#appVersion");
    if (ver) {
      const v = st.health && st.health.version;
      ver.textContent = v ? "v" + v : "—";
    }
  }

  /* ------------------------------------------------------------------ #
   * 错误条 / 脏条
   * ------------------------------------------------------------------ */

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

  function renderDirty() {
    const bar = U.$("#dirty-bar");
    if (!bar) return;
    bar.classList.toggle("hidden", !st.dirty);
  }

  /* ------------------------------------------------------------------ #
   * 通用片段
   * ------------------------------------------------------------------ */

  function emptyState(icon, title, sub) {
    return (
      '<div class="empty">' +
      (icon ? '<div class="empty-icon"></div>' : "") +
      "<h4>" + esc(title || "暂无数据") + "</h4>" +
      (sub ? "<p>" + esc(sub) + "</p>" : "") +
      "</div>"
    );
  }

  function metric(label, value, unit, foot, footCls) {
    return (
      '<div class="metric">' +
      '<div class="metric-label">' + esc(label) + "</div>" +
      '<div class="metric-value">' + esc(value) +
      (unit ? '<span class="unit">' + esc(unit) + "</span>" : "") +
      "</div>" +
      '<div class="metric-foot ' + esc(footCls || "") + '">' + esc(foot || "") + "</div>" +
      "</div>"
    );
  }

  /**
   * 命中卡。
   * @param {object} r S.deco() 派生后的记录
   * @param {{fresh?:boolean, actions?:boolean}} opts
   */
  function hitCard(r, opts) {
    const o = opts || {};
    const fresh = !!o.fresh || !!st.freshIds[r.product_id];
    const cls =
      "hitcard" +
      (fresh ? " fresh" : "") +
      (r.sold ? " sold" : "") +
      (!r.sold && !r.isHit ? " over" : "");

    const titleHtml = r.url
      ? '<a href="' + esc(r.url) + '" target="_blank" rel="noopener">' + esc(r.title) + "</a>"
      : esc(r.title);

    const metaBits = [];
    metaBits.push('<span class="badge kw">' + esc(r.keyword || "未标关键词") + "</span>");
    metaBits.push("<span>" + esc(U.relTime(r.time)) + "</span>");
    if (r.publish) metaBits.push("<span>发布 " + esc(U.relTime(r.publish)) + "</span>");
    if (r.sold) {
      metaBits.push('<span class="badge off">已售出/下架</span>');
    } else if (r.thresh === null) {
      metaBits.push('<span class="badge off">阈值已移除</span>');
    } else if (r.isHit) {
      metaBits.push('<span class="badge hit">低于阈值 ' + esc(U.money(r.below)) + "</span>");
    } else {
      metaBits.push('<span class="badge warn">已高于阈值 ' + esc(U.money(-r.below)) + "</span>");
    }

    let delta = "";
    if (r.thresh !== null) {
      delta = "阈值 " + U.money(r.thresh) + " · 差 " + (r.below >= 0 ? "-" : "+") + U.money(Math.abs(r.below));
    } else {
      delta = "无对应阈值";
    }

    const actions = o.actions === false
      ? ""
      : '<div class="hit-actions">' +
        (r.url
          ? '<a class="btn sm" href="' + esc(r.url) + '" target="_blank" rel="noopener">打开</a>'
          : "") +
        '<button class="btn sm" type="button" data-action="' +
        (r.sold ? "unmark-one" : "sold-one") +
        '">' + (r.sold ? "恢复在架" : "已售出") + "</button>" +
        '<button class="btn sm danger-ghost" type="button" data-action="black-one">拉黑</button>' +
        "</div>";

    return (
      '<div class="' + cls + '" data-id="' + esc(r.product_id) + '">' +
      (o.actions === false
        ? ""
        : '<input type="checkbox" class="hit-check" data-select value="' + esc(r.product_id) + '"' +
          (st.selected[r.product_id] ? " checked" : "") +
          (r.sold ? " disabled" : "") + ">") +
      '<div class="hit-main">' +
      '<div class="hit-title">' + titleHtml + "</div>" +
      '<div class="hit-meta">' + metaBits.join("") + "</div>" +
      "</div>" +
      '<div class="hit-price">' +
      "<b" + (!r.sold && !r.isHit ? ' class="over"' : "") + ">" + esc(U.money(r.price)) + "</b>" +
      '<div class="hit-delta">' + esc(delta) + "</div>" +
      "</div>" +
      actions +
      "</div>"
    );
  }

  /* ------------------------------------------------------------------ #
   * 视图切换 / 导航
   * ------------------------------------------------------------------ */

  function setView(name, opts) {
    if (VIEWS.indexOf(name) < 0) name = "overview";
    st.view = name;
    U.$$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
    VIEWS.forEach((v) => {
      const el = U.$("#view-" + v);
      if (el) el.classList.toggle("active", v === name);
    });
    try {
      if (location.hash !== "#" + name && !(opts && opts.silent)) {
        history.replaceState(null, "", "#" + name);
      }
    } catch (e) {
      /* file:// 或受限环境忽略 */
    }
    document.title = VIEW_TITLES[name] + " · 闲鱼低价提醒工具";
    window.dispatchEvent(new CustomEvent("xy:view", { detail: { view: name } }));
  }

  /** 从 location.hash 解析初始视图。 */
  function initialView() {
    const h = String(location.hash || "").replace(/^#/, "");
    return VIEWS.indexOf(h) >= 0 ? h : "overview";
  }

  function navigate(name) {
    setView(name);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return {
    VIEWS: VIEWS,
    VIEW_TITLES: VIEW_TITLES,
    esc: esc,
    renderTopbar: renderTopbar,
    showError: showError,
    hideError: hideError,
    renderDirty: renderDirty,
    emptyState: emptyState,
    metric: metric,
    hitCard: hitCard,
    setView: setView,
    initialView: initialView,
    navigate: navigate,
  };
})();
