#!/usr/bin/env node
/* ===========================================================
 * scripts/web_contract_probe.js —— 前端契约探针
 *
 * 目的：不做静态文本匹配，而是**真的在无 DOM 环境里加载前端模块**，
 *       调用数据层每一个函数，捕获它实际发出的 URL/Method，
 *       再把前端的通道元数据一并导出成 JSON，交给 Python 侧与后端源码比对。
 *
 * 用法：node scripts/web_contract_probe.js
 * 输出：stdout 一行 JSON
 * =========================================================== */
"use strict";

const fs = require("fs");
const path = require("path");

const STATIC = path.join(__dirname, "..", "web", "static");
const JS = path.join(STATIC, "js");

/* ---------------- 无 DOM 运行时桩 ---------------- */

const calls = [];
global.window = global;
global.location = { hash: "" };
global.document = {
  addEventListener() {},
  querySelector() { return null; },
  querySelectorAll() { return []; },
  createElement() {
    return { style: {}, dataset: {}, classList: { add() {}, remove() {}, toggle() {} },
      appendChild() {}, addEventListener() {}, setAttribute() {} };
  },
};
global.localStorage = {
  _d: {},
  getItem(k) { return this._d[k] || null; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
};
global.setTimeout = () => 0;   // 掐掉 SSE 重连定时器，避免进程挂住
global.clearTimeout = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};

global.fetch = async function (url, opts) {
  calls.push({ url: String(url), method: (opts && opts.method) || "GET" });
  return {
    ok: true,
    status: 200,
    body: null,
    json: async () => ({ ok: true }),
  };
};

/* ---------------- 按 index.html 的顺序加载模块 ---------------- */

const ORDER = ["util.js", "api.js", "modal.js", "state.js", "ui.js", "data.js", "view-targets.js"];
for (const f of ORDER) {
  const code = fs.readFileSync(path.join(JS, f), "utf8");
  try {
    // eslint-disable-next-line no-eval
    eval(code);
  } catch (e) {
    process.stderr.write("加载 " + f + " 失败：" + e.message + "\n");
    process.exit(1);
  }
}

const D = global.XY.Data;
const S = global.XY.State;
const VT = global.XY.ViewTargets;

/* ---------------- 逐个调用数据层函数 ---------------- */

const PROBES = [
  ["health", () => D.health()],
  ["loadConfig", () => D.loadConfig()],
  ["saveConfig", () => D.saveConfig({})],
  ["status", () => D.status()],
  ["startMonitor", () => D.startMonitor()],
  ["stopMonitor", () => D.stopMonitor()],
  ["runOnce", () => D.runOnce()],
  ["setDetailOnly", () => D.setDetailOnly(true)],
  ["saveCookie", () => D.saveCookie("x=1")],
  ["refreshCookie", () => D.refreshCookie()],
  ["loadPool", () => D.loadPool()],
  ["poolAction", () => D.poolAction({ action: "toggle", name: "a" })],
  ["testNotify", () => D.testNotify("console", {})],
  ["loadRecords", () => D.loadRecords({ includeSold: true, limit: 50, sort: "time", order: "desc" })],
  ["checkShelf", () => D.checkShelf(["779001", "779002"])],
  ["checkShelfStatus", () => D.checkShelfStatus()],
  ["cancelCheckShelf", () => D.cancelCheckShelf()],
  ["clearRecords", () => D.clearRecords()],
  ["markSold", () => D.markSold("779001")],
  ["unmarkSold", () => D.unmarkSold("779001")],
  ["blacklist", () => D.blacklist("779001", "人工剔除")],
  ["loadBlacklist", () => D.loadBlacklist(200)],
  ["restoreBlacklist", () => D.restoreBlacklist("779001")],
  ["connectLogStream", () => { D.connectLogStream({}); return Promise.resolve(); }],
];

async function main() {
  const out = [];
  for (const [name, fn] of PROBES) {
    const before = calls.length;
    try {
      await fn();
    } catch (e) {
      /* 桩环境下的预期异常（如 SSE 无 body）忽略，URL 仍已记录 */
    }
    const made = calls.slice(before);
    out.push({
      fn: name,
      calls: made,
      err: calls.length === before ? "未发出任何请求" : null,
    });
  }

  // 缺口检测：数据层导出了哪些函数，但探针没覆盖
  const exported = Object.keys(D).sort();
  const probed = PROBES.map((p) => p[0]);
  const uncovered = exported.filter((k) => probed.indexOf(k) < 0);

  const payload = {
    ok: true,
    functions: out,
    exported: exported,
    uncovered: uncovered,
    channelOrder: S.CHANNEL_ORDER,
    channelLabels: S.CHANNEL_LABELS,
    channelTags: S.CHANNEL_TAGS,
    // [key,label,isSecret,default] → 归一成 {key,label,secret,def}
    channelFields: Object.keys(S.CHANNEL_FIELDS).reduce((acc, k) => {
      acc[k] = S.CHANNEL_FIELDS[k].map((f) => ({ key: f[0], label: f[1], secret: !!f[2], def: f[3] }));
      return acc;
    }, {}),
    cookieStates: S.COOKIE_STATES,

    // PUT /api/config 的表单体：真实跑一遍 buildForm()，导出它产出的键
    form: (function () {
      S.s.config = {
        keywords: [
          { keyword: "Switch", max_price: 1000, enabled: true, exclude_keywords: ["收"], required_keywords: ["港版"] },
        ],
        interval_seconds: 600,
        fetcher_type: "mtop",
        pages: 1,
        page_size: 30,
        page_sleep: 2,
        user_agent: "",
        storage_path: "state/xianyu_alert.db",
        channels: { console: { enabled: true, options: {} } },
        cookie_alert_enabled: true,
        cookie_check_interval_seconds: 0,
        preset_exclude_keywords: ["收"],
      };
      const f = VT.buildForm();
      return { keys: Object.keys(f).sort(), keywordKeys: Object.keys(f.keywords[0]).sort(), sample: f };
    })(),

    // 记录派生：喂一条「只含后端字段」的记录，导出派生结果
    decoSample: (function () {
      const rec = {
        keyword: "Switch",
        product_id: "779001",
        title: "Switch OLED 港版",
        price: 880,
        url: "https://www.goofish.com/item?id=779001",
        publish_time: "2026-09-21 10:00:00",
        first_seen: "2026-09-21 09:00:00",
        last_seen: "2026-09-21 12:00:00",
        notified: 1,
        sold_out: 0,
        sold_at: "",
        sold_reason: "",
        time: "2026-09-21 12:00:00",
        publish: "2026-09-21 10:00:00",
      };
      const d = S.deco(rec);
      return {
        inputKeys: Object.keys(rec).sort(),
        outputKeys: Object.keys(d).sort(),
        sample: d,
      };
    })(),
  };
  process.stdout.write(JSON.stringify(payload, null, 2) + "\n");
  process.exit(0);
}

main();
