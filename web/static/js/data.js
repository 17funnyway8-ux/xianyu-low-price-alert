/* ===========================================================
 * 闲鱼低价提醒工具 —— data.js（真实接口数据层）
 * 挂载到 window.XY.Data。
 *
 * 唯一职责：把 web/api.py 暴露的每个路由包装成一个语义化函数。
 * 视图层只调用本文件，绝不直接拼 URL —— 这样接口改动只有一处要改。
 *
 * 契约来源：web/api.py
 *   GET  /healthz                        免认证
 *   GET  /api/config                     → ok(form)
 *   PUT  /api/config                     body {form}
 *   POST /api/monitor/start|stop|run_once
 *   GET  /api/monitor/status
 *   POST /api/monitor/detail_only        body {enabled}
 *   POST /api/cookie/save                body {cookie}
 *   GET  /api/cookie/pool
 *   POST /api/cookie/pool                body {action,name,new_name,cookie,force_missing_token}
 *   POST /api/notify/test                body {channel_type,options}
 *   GET  /api/records                    ?include_sold&limit&sort&order
 *   POST /api/records/check_shelf        body {product_ids}  → 202
 *   GET  /api/records/check_shelf/status
 *   POST /api/records/check_shelf/cancel
 *   POST /api/records/clear
 *   POST /api/records/{id}/sold | unmark | blacklist
 *   GET  /api/blacklist                  ?limit
 *   POST /api/blacklist/{id}/restore
 *   GET  /api/logs/stream                SSE
 * =========================================================== */
window.XY = window.XY || {};
window.XY.Data = (function () {
  "use strict";
  const Api = window.XY.Api;

  /** 路径片段安全编码（product_id 来自后端，仍按不可信处理）。 */
  function enc(v) {
    return encodeURIComponent(String(v == null ? "" : v));
  }

  /** 拼 query string（跳过 undefined/空串）。 */
  function qs(params) {
    const parts = [];
    Object.keys(params || {}).forEach((k) => {
      const v = params[k];
      if (v === undefined || v === null || v === "") return;
      parts.push(enc(k) + "=" + enc(v));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  /* ---------------- 健康 / 配置 ---------------- */

  function health() {
    return Api.api("/healthz");
  }

  function loadConfig() {
    return Api.api("/api/config");
  }

  /**
   * 保存配置。
   * @param {object} form 与 GET /api/config 同构的表单
   * @returns {Promise<{ok:boolean,restarted:boolean,message:string}>}
   */
  function saveConfig(form) {
    return Api.api("/api/config", { method: "PUT", body: { form: form } });
  }

  /* ---------------- 运行状态 ---------------- */

  function status() {
    return Api.api("/api/monitor/status");
  }

  function startMonitor() {
    return Api.api("/api/monitor/start", { method: "POST" });
  }

  function stopMonitor() {
    return Api.api("/api/monitor/stop", { method: "POST" });
  }

  function runOnce() {
    return Api.api("/api/monitor/run_once", { method: "POST" });
  }

  /**
   * 明细日志开关。
   * 注意：后端字段语义是 detail_only=true → 仅展示命中。
   */
  function setDetailOnly(enabled) {
    return Api.api("/api/monitor/detail_only", { method: "POST", body: { enabled: !!enabled } });
  }

  /* ---------------- Cookie ---------------- */

  /** v1.9：Cookie 分层诊断 + 保活状态。 */
  function cookieStatus() {
    return Api.api("/api/cookie/status");
  }

  function saveCookie(cookie) {
    return Api.api("/api/cookie/save", { method: "POST", body: { cookie: cookie } });
  }

  /**
   * 免扫码刷新 Cookie（用服务端的浏览器持久化 profile）。
   * 服务端未安装 Playwright 时后端会返回 400 并在 message 里给出替代方案，
   * 前端直接把该 message 展示给用户即可。
   */
  function refreshCookie() {
    return Api.api("/api/cookie/refresh", { method: "POST" });
  }

  function loadPool() {
    return Api.api("/api/cookie/pool");
  }

  /**
   * Cookie 池操作。
   * @param {{action:string,name?:string,new_name?:string,cookie?:string,force_missing_token?:boolean}} body
   */
  function poolAction(body) {
    return Api.api("/api/cookie/pool", { method: "POST", body: body });
  }

  /* ---------------- 通知 ---------------- */

  function testNotify(channelType, options) {
    return Api.api("/api/notify/test", {
      method: "POST",
      body: { channel_type: channelType, options: options || {} },
    });
  }

  /* ---------------- 记录 ---------------- */

  /**
   * 拉取提醒记录。
   * @param {{includeSold?:boolean,limit?:number,sort?:string,order?:string}} opts
   * @returns {Promise<{ok:boolean,records:Array,total:number}>}
   */
  function loadRecords(opts) {
    const o = opts || {};
    return Api.api(
      "/api/records" +
        qs({
          include_sold: o.includeSold ? "true" : "false",
          limit: o.limit || 200,
          sort: o.sort || "time",
          order: o.order || "desc",
        })
    );
  }

  function checkShelf(productIds) {
    return Api.api("/api/records/check_shelf", {
      method: "POST",
      body: { product_ids: productIds || [] },
    });
  }

  function checkShelfStatus() {
    return Api.api("/api/records/check_shelf/status");
  }

  function cancelCheckShelf() {
    return Api.api("/api/records/check_shelf/cancel", { method: "POST" });
  }

  function clearRecords() {
    return Api.api("/api/records/clear", { method: "POST" });
  }

  function markSold(productId) {
    return Api.api("/api/records/" + enc(productId) + "/sold", { method: "POST" });
  }

  function unmarkSold(productId) {
    return Api.api("/api/records/" + enc(productId) + "/unmark", { method: "POST" });
  }

  function blacklist(productId, reason) {
    return Api.api("/api/records/" + enc(productId) + "/blacklist", {
      method: "POST",
      body: { reason: reason || "人工剔除" },
    });
  }

  /* ---------------- 黑名单 ---------------- */

  function loadBlacklist(limit) {
    return Api.api("/api/blacklist" + qs({ limit: limit || 200 }));
  }

  function restoreBlacklist(productId) {
    return Api.api("/api/blacklist/" + enc(productId) + "/restore", { method: "POST" });
  }

  /* ---------------- SSE 日志 ---------------- */

  /**
   * 订阅实时日志流。
   * @param {{onMessage:(e:object)=>void,onStatus:(s:string)=>void,onError:(e:Error)=>void}} handlers
   */
  function connectLogStream(handlers) {
    return Api.connectStream("/api/logs/stream", handlers);
  }

  return {
    health: health,
    loadConfig: loadConfig,
    saveConfig: saveConfig,

    status: status,
    startMonitor: startMonitor,
    stopMonitor: stopMonitor,
    runOnce: runOnce,
    setDetailOnly: setDetailOnly,

    saveCookie: saveCookie,
    cookieStatus: cookieStatus,
    refreshCookie: refreshCookie,
    loadPool: loadPool,
    poolAction: poolAction,

    testNotify: testNotify,

    loadRecords: loadRecords,
    checkShelf: checkShelf,
    checkShelfStatus: checkShelfStatus,
    cancelCheckShelf: cancelCheckShelf,
    clearRecords: clearRecords,
    markSold: markSold,
    unmarkSold: unmarkSold,
    blacklist: blacklist,

    loadBlacklist: loadBlacklist,
    restoreBlacklist: restoreBlacklist,

    connectLogStream: connectLogStream,
  };
})();
