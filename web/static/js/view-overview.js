/* ===========================================================
 * 闲鱼低价提醒工具 —— view-overview.js（① 态势总览）
 * 挂载到 window.XY.ViewOverview。
 *
 * 首屏回答一个问题：我蹲的东西，离我的心理价还差多少？
 * 数据来源：/api/config（阈值） + /api/records（现价） + /api/monitor/status
 * =========================================================== */
window.XY = window.XY || {};
window.XY.ViewOverview = (function () {
  "use strict";
  const U = window.XY.Util;
  const S = window.XY.State;
  const UI = window.XY.UI;
  const st = S.s;
  const esc = U.escapeHtml;

  /* ---------------- 指标卡 ---------------- */

  function renderMetrics() {
    const box = U.$("#ovMetrics");
    if (!box) return;

    const kws = S.keywords();
    const enabled = kws.filter((k) => k.enabled !== false);
    const disabled = kws.length - enabled.length;
    const today = S.todayHits();
    const cl = S.closestToLine();
    const meta = S.cookieMeta();
    const cfg = st.config;

    // ① 在蹲关键词
    const m1 = UI.metric(
      "在蹲关键词",
      String(enabled.length),
      "个",
      disabled ? "另有 " + disabled + " 个已停用" : "全部启用",
      ""
    );

    // ② 今日命中
    const total = (st.status && st.status.notified_count) || 0;
    const m2 = UI.metric(
      "今日命中",
      String(today.length),
      "件",
      "累计提醒 " + total + " 件",
      today.length ? "up" : ""
    );

    // ③ 距破线最近（ratio = 阈值 ÷ 现价，100% 即到达阈值）
    let m3;
    if (cl.best) {
      const p = cl.best;
      m3 = UI.metric(
        "距破线最近",
        U.pct(p.ratio),
        "",
        p.keyword + " · 现价 " + U.money(p.price),
        p.hit ? "up" : ""
      );
    } else {
      m3 = UI.metric("距破线最近", "—", "", "尚无观测价（需先有命中）", "");
    }

    // ④ Cookie
    const m4 = UI.metric(
      "登录 Cookie",
      cfg ? meta.brief.replace(/^Cookie\s*/, "") : "读取中",
      "",
      cfg && cfg.cookies_masked ? cfg.cookies_masked : (cfg ? S.cookieHealth().text || "" : ""),
      S.dotTone(meta.dot)
    );

    box.innerHTML = m1 + m2 + m3 + m4;
  }

  /* ---------------- Cookie 警示条 ---------------- */

  function renderCookieAlert() {
    const el = U.$("#ovCookieAlert");
    if (!el) return;
    const cfg = st.config;
    const health = S.cookieHealth();
    const meta = S.cookieMeta();
    const hide = !cfg || health.state === "ok" || health.state === "missing";
    el.classList.toggle("hidden", hide);
    if (hide) return;
    el.classList.toggle("warn", health.state === "expiring");
    const dot = U.$("#ovCookieDot");
    if (dot) dot.className = "dot " + (health.state === "expiring" ? "warn" : "err");
    const title = U.$("#ovCookieTitle");
    const text = U.$("#ovCookieText");
    if (title) title.textContent = meta.brief + " —— 监控会静默失效";
    if (text) text.textContent = health.text || "";
  }

  /* ---------------- 狙击进度 ---------------- */

  function targetRow(kwCfg) {
    const kw = kwCfg.keyword;
    const thresh = Number(kwCfg.max_price) || 0;
    const p = S.progressOf(kw);

    if (!p) {
      // 没有任何命中记录 → 后端不返回未命中商品的价格，无法计算距离（如实说明）
      return (
        '<div class="target">' +
        '<div><div class="target-name">' + esc(kw) + '</div>' +
        '<div class="target-thresh">阈值 ' + esc(U.money(thresh)) + "</div></div>" +
        '<div><div class="bar"><div class="bar-fill" style="width:0%"></div></div>' +
        '<div class="target-meta nodata">尚未命中，暂无观测价（记录接口只返回已命中商品）</div></div>' +
        '<div class="target-right"><span class="badge off">暂无数据</span></div>' +
        "</div>"
      );
    }

    const width = Math.max(2, Math.min(100, p.ratio * 100));
    const gapText = p.hit
      ? "现价 " + U.money(p.price) + " · 已低于阈值 " + U.money(-p.gap)
      : "现价 " + U.money(p.price) + " · 还差 " + U.money(p.gap);
    const badge = p.hit
      ? '<span class="badge hit">已破线</span>'
      : '<span class="badge">未破线</span>';

    return (
      '<div class="target' + (p.hit ? " hitrow" : "") + '">' +
      '<div><div class="target-name">' + esc(kw) + "</div>" +
      '<div class="target-thresh">阈值 ' + esc(U.money(thresh)) + "</div></div>" +
      '<div><div class="bar"><div class="bar-fill' + (p.hit ? " hit" : "") +
      '" style="width:' + width.toFixed(1) + '%"></div></div>' +
      '<div class="target-meta' + (p.hit ? " hit" : "") + '">' + esc(gapText) +
      " · 进度 " + esc(U.pct(p.ratio)) + "</div></div>" +
      '<div class="target-right">' + badge + "</div>" +
      "</div>"
    );
  }

  function renderTargets() {
    const box = U.$("#ovTargetList");
    if (!box) return;
    const kws = S.keywords().filter((k) => k.enabled !== false);

    if (!kws.length) {
      box.innerHTML = UI.emptyState(
        true,
        st.config ? "还没有在蹲的关键词" : "正在读取配置…",
        st.config ? "去「监控配置」添加关键词与价格阈值" : ""
      );
      return;
    }

    // 已破线在前，其次按 ratio 降序（越接近阈值越靠前）
    const sorted = kws.slice().sort((a, b) => {
      const pa = S.progressOf(a.keyword);
      const pb = S.progressOf(b.keyword);
      const ra = pa ? pa.ratio : -1;
      const rb = pb ? pb.ratio : -1;
      return rb - ra;
    });
    box.innerHTML = sorted.map(targetRow).join("");

    const sub = U.$("#ovTargetsSub");
    if (sub) {
      const cl = S.closestToLine();
      sub.textContent =
        "已破线 " + cl.hits + " / " + kws.length + " 个关键词";
    }
  }

  /* ---------------- 今日命中 ---------------- */

  function renderHits() {
    const box = U.$("#ovHitList");
    if (!box) return;
    const list = S.todayHits();

    if (!list.length) {
      box.innerHTML = UI.emptyState(
        true,
        "今天还没有命中",
        st.status && st.status.running ? "监控运行中，命中后会立刻出现在这里" : "监控未运行 —— 点右上角开始"
      );
      const sub = U.$("#ovHitsSub");
      if (sub) sub.textContent = "";
      return;
    }

    box.innerHTML = list.slice(0, 6).map((r) => UI.hitCard(r, { actions: false, fresh: true })).join("");
    const sub = U.$("#ovHitsSub");
    if (sub) {
      sub.textContent = list.length > 6 ? "显示最近 6 条 / 共 " + list.length + " 条" : "共 " + list.length + " 条";
    }
  }

  /* ---------------- 运行条 ---------------- */

  function renderRunStrip() {
    const stt = st.status || {};
    const set = (id, v) => {
      const el = U.$(id);
      if (el) el.textContent = v;
    };
    set("#ovRunState", stt.running ? "运行中" : "未运行");
    set("#ovRoundCount", String(stt.round_count || 0));
    set("#ovNotified", String(stt.notified_count || 0));

    let next = "—";
    if (stt.running) {
      const secs = st.countdown === null || st.countdown === undefined ? stt.next_round_in : st.countdown;
      next = secs === null || secs === undefined ? "—" : U.clockText(secs);
    }
    set("#ovNextRound", next);

    const btn = U.$("#ovRunOnceBtn");
    if (btn) {
      btn.disabled = !!stt.running;
      btn.title = stt.running ? "监控运行中不可手动执行（后端返回 409）" : "立即执行一轮监测";
    }
  }

  /* ---------------- 汇总 ---------------- */

  function render() {
    renderCookieAlert();
    renderMetrics();
    renderTargets();
    renderHits();
    renderRunStrip();
  }

  /* ---------------- 事件 ---------------- */

  function init() {
    const pulse = U.$("#pulseBtn");
    if (pulse) {
      pulse.addEventListener("click", async () => {
        const running = !!(st.status && st.status.running);
        U.setBtnLoading(pulse, true);
        try {
          const res = running
            ? await window.XY.Data.stopMonitor()
            : await window.XY.Data.startMonitor();
          U.toast(res.message || (running ? "监测已停止" : "监测已启动"), "hit");
          window.dispatchEvent(new CustomEvent("xy:refresh-status"));
        } catch (e) {
          U.toast(e.message, true);
        } finally {
          U.setBtnLoading(pulse, false);
          window.XY.UI.renderTopbar();
        }
      });
    }

    const health = U.$("#healthBtn");
    if (health) health.addEventListener("click", () => UI.navigate("targets"));

    const go = U.$("#ovCookieGoBtn");
    if (go) go.addEventListener("click", () => UI.navigate("targets"));

    const allBtn = U.$("#ovHitsAllBtn");
    if (allBtn) {
      allBtn.addEventListener("click", () => {
        st.filters.todayOnly = true;
        const cb = U.$("#hitsTodayOnly");
        if (cb) cb.checked = true;
        UI.navigate("hits");
        window.dispatchEvent(new CustomEvent("xy:refresh-hits"));
      });
    }

    const once = U.$("#ovRunOnceBtn");
    if (once) {
      once.addEventListener("click", async () => {
        U.setBtnLoading(once, true);
        try {
          const res = await window.XY.Data.runOnce();
          U.toast(res.message || "单轮执行完成", res.notified ? "hit" : "info");
          window.dispatchEvent(new CustomEvent("xy:refresh-data"));
        } catch (e) {
          U.toast(e.message, true);
        } finally {
          U.setBtnLoading(once, false);
          renderRunStrip();
        }
      });
    }
  }

  return { init: init, render: render, renderRunStrip: renderRunStrip };
})();
