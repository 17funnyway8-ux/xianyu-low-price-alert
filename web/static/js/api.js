/* ===========================================================
 * 闲鱼低价提醒工具 —— api.js（fetch 封装 + 认证 + SSE 流式订阅）
 * 挂载到 window.XY.Api。
 *
 * 设计（P3-01）：
 *   - api() 统一附带 Authorization: Bearer <localStorage token>；
 *   - 收到 401 → 清 token → 弹认证遮罩（auth.js）→ 抛错；
 *   - connectStream() 用 fetch + ReadableStream 解析 SSE（EventSource
 *     无法携带 Authorization 头），断线指数退避自动重连（2s→5s→10s 封顶）。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.Api = (function () {
  "use strict";

  const TOKEN_KEY = "xy_web_token";

  function getToken() {
    try {
      return localStorage.getItem(TOKEN_KEY) || "";
    } catch (e) {
      return "";
    }
  }

  function setToken(t) {
    try {
      if (t) localStorage.setItem(TOKEN_KEY, t);
      else localStorage.removeItem(TOKEN_KEY);
    } catch (e) {
      /* localStorage 不可用时静默 */
    }
  }

  function clearToken() {
    setToken("");
  }

  /**
   * 统一 API 请求封装。
   * @param {string} path 接口路径（以 /api 开头）。
   * @param {{method?: string, body?: object}} options
   * @returns {Promise<object>} 统一信封 {ok:true, ...}。
   * @throws {Error} 401 / 业务失败 / 网络失败（message 为中文原因）。
   */
  async function api(path, options) {
    const opts = {
      method: (options && options.method) || "GET",
      headers: { "Content-Type": "application/json" },
    };
    const token = getToken();
    if (token) opts.headers.Authorization = "Bearer " + token;
    if (options && options.body !== undefined) {
      opts.body = JSON.stringify(options.body);
    }
    let resp;
    try {
      resp = await fetch(path, opts);
    } catch (e) {
      throw new Error("网络请求失败：" + e.message);
    }
    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
    if (resp.status === 401) {
      clearToken();
      if (window.XY.Auth) window.XY.Auth.onUnauthorized();
      throw new Error((data && data.message) || "未认证或 token 错误");
    }
    if (!resp.ok || (data && data.ok === false)) {
      throw new Error((data && data.message) || "HTTP " + resp.status);
    }
    return data || {};
  }

  /**
   * fetch 流式 SSE 订阅（可携带 Authorization 头 + 自动重连）。
   * @param {string} path SSE 路径（如 /api/logs/stream）。
   * @param {{onMessage?: (entry: object)=>void, onStatus?: (s: string)=>void,
   *          onError?: (e: Error)=>void}} handlers
   * @returns {{stop: ()=>void}} 控制句柄。
   */
  function connectStream(path, handlers) {
    const h = handlers || {};
    let stopped = false;
    let retryDelay = 2000;
    let timer = null;

    async function run() {
      while (!stopped) {
        try {
          const token = getToken();
          const headers = { cache: "no-store" };
          if (token) headers.Authorization = "Bearer " + token;
          const resp = await fetch(path, { headers });
          if (resp.status === 401) {
            clearToken();
            if (window.XY.Auth) window.XY.Auth.onUnauthorized();
            return; // 认证遮罩提交成功后由事件驱动重连
          }
          if (!resp.ok || !resp.body) {
            throw new Error("HTTP " + resp.status);
          }
          retryDelay = 2000; // 连接成功重置退避
          if (h.onStatus) h.onStatus("connected");
          const reader = resp.body.getReader();
          const decoder = new TextDecoder("utf-8");
          let buffer = "";
          while (!stopped) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf("\n\n")) >= 0) {
              const chunk = buffer.slice(0, idx);
              buffer = buffer.slice(idx + 2);
              chunk.split("\n").forEach((line) => {
                if (line.startsWith("data: ")) {
                  try {
                    const entry = JSON.parse(line.slice(6));
                    if (h.onMessage) h.onMessage(entry);
                  } catch (e) {
                    /* 忽略非 JSON 数据（心跳等） */
                  }
                }
              });
            }
          }
        } catch (e) {
          if (h.onError) h.onError(e);
        }
        if (stopped) break;
        if (h.onStatus) h.onStatus("reconnecting");
        await new Promise((resolve) => {
          timer = setTimeout(resolve, retryDelay);
        });
        retryDelay = Math.min(retryDelay * 2, 10000);
      }
    }

    run();
    return {
      stop() {
        stopped = true;
        if (timer) clearTimeout(timer);
      },
    };
  }

  return {
    api: api,
    connectStream: connectStream,
    getToken: getToken,
    setToken: setToken,
    clearToken: clearToken,
  };
})();
