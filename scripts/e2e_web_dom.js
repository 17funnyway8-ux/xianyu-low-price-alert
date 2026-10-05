#!/usr/bin/env node
/* ===========================================================
 * scripts/e2e_web_dom.js —— 前端 × 真实后端的 DOM 端到端验证
 *
 * 做法：起真实后端（uvicorn + mock 抓取器），用 jsdom 以
 *       runScripts:"dangerously" + resources:"usable" 加载真实的
 *       http://127.0.0.1:PORT/ ，让 index.html 里那串 <script> 真的执行，
 *       前端用真实的 /api/* 请求取数，然后断言 DOM 渲染结果。
 *
 * 关键点：jsdom 不提供 fetch，必须在 beforeParse 里注入 —— 若等到
 *         JSDOM 构造完再注入，页面脚本可能已经先跑并调用了 fetch。
 *
 * 这一层能抓到静态检查抓不到的东西：
 *   - 视图渲染时的运行时报错（未定义字段、null 解引用）
 *   - 选择器与 HTML 不匹配（按钮点了没反应）
 *   - 接口返回结构与视图假设不符
 *   - 真实的 UI 状态（如非 mtop 下「校验在架」必须禁用）
 *
 * 用法：NODE_PATH=<jsdom 所在 node_modules> node scripts/e2e_web_dom.js
 * 退出码：0 = 全绿；1 = 有失败；2 = 缺 jsdom
 * =========================================================== */
"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const net = require("net");
const os = require("os");
const path = require("path");

let JSDOM;
let VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require("jsdom"));
} catch (e) {
  console.error("缺少 jsdom。请安装：npm i jsdom（或用 NODE_PATH 指向已装目录）");
  process.exit(2);
}

const ROOT = path.dirname(__dirname);
const PY_CANDIDATES = [
  "/Users/xxx/.workbuddy/binaries/python/envs/default/bin/python",
  path.join(ROOT, ".venv", "bin", "python"),
];

const CONFIG_YAML = `\
keywords:
  - keyword: Switch
    max_price: 1500
    exclude_keywords: [收]
    required_keywords: []
  - keyword: Kindle
    max_price: 300
monitor:
  interval_seconds: 120
  user_agent: ''
  cookies: ''
  cookie_alert_enabled: true
  cookie_check_interval_seconds: 0
fetcher:
  type: mock
  pages: 1
  page_size: 30
  page_sleep: 0
  mock_products_per_round: 6
storage:
  path: state/xianyu_alert.db
notify:
  channels:
    - type: console
`;

const ROWS = [];
function check(name, cond, detail) {
  ROWS.push({ name, ok: !!cond, detail: detail === undefined ? "" : String(detail) });
  return !!cond;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
}

function pickPython() {
  for (const p of PY_CANDIDATES) if (fs.existsSync(p)) return p;
  return "python3";
}

const LAUNCHER = (root, data, port) => `
import os, sys
sys.path.insert(0, ${JSON.stringify(root)})
os.environ["XY_DATA_DIR"] = ${JSON.stringify(data)}
from xianyu_alert import paths, secure
paths.ensure_data_dir()
secure._load_or_create_key()
from xianyu_alert.cli import setup_logging
setup_logging(verbose=False)
from web import api
import uvicorn
uvicorn.run(api.app, host="127.0.0.1", port=${port}, log_level="warning", access_log=False)
`;

(async function main() {
  const py = pickPython();
  const port = await freePort();
  const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), "xy-dom-"));
  fs.writeFileSync(path.join(dataDir, "config.yaml"), CONFIG_YAML, "utf8");
  const base = `http://127.0.0.1:${port}`;

  console.log(`python   : ${py}`);
  console.log(`端口     : ${port}`);
  console.log(`数据目录 : ${dataDir}`);

  const child = spawn(py, ["-c", LAUNCHER(ROOT, dataDir, port)], {
    cwd: ROOT,
    stdio: ["ignore", "pipe", "pipe"],
  });
  let serverOut = "";
  child.stdout.on("data", (d) => (serverOut += d.toString()));
  child.stderr.on("data", (d) => (serverOut += d.toString()));
  const cleanup = () => {
    try {
      child.kill("SIGTERM");
    } catch (e) {
      /* ignore */
    }
  };

  try {
    let up = false;
    const deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      if (child.exitCode !== null) break;
      try {
        const r = await fetch(base + "/healthz");
        if (r.ok) {
          up = true;
          break;
        }
      } catch (e) {
        await sleep(300);
      }
    }
    if (!check("后端启动可达", up, base + "/healthz")) {
      console.error("\n后端未启动，输出：\n" + serverOut.slice(0, 3000));
      cleanup();
      process.exit(1);
    }

    const onceRes = await fetch(base + "/api/monitor/run_once", { method: "POST" });
    const onceJson = await onceRes.json();
    check("预置一轮监测产出命中", onceJson.ok && onceJson.notified >= 1, `notified=${onceJson.notified}`);

    /* ---------------- 装载真实前端 ---------------- */

    const pageErrors = [];
    const consoleErrors = [];
    const badResponses = [];
    const nativeFetch = fetch;

    const vc = new VirtualConsole();
    vc.on("jsdomError", (err) => {
      const msg = String((err && err.message) || err);
      pageErrors.push({
        type: /Could not parse CSS/i.test(msg) ? "css" : "jsdom",
        msg,
      });
    });
    vc.on("error", (...args) => consoleErrors.push(args.map(String).join(" ")));

    const dom = await JSDOM.fromURL(base + "/", {
      runScripts: "dangerously",
      resources: "usable",
      pretendToBeVisual: true,
      virtualConsole: vc,
      // 必须在页面脚本执行前注入宿主能力（jsdom 不提供 fetch / Streams）
      beforeParse(window) {
        window.fetch = (input, init) => {
          const url = typeof input === "string" ? new URL(input, base).toString() : input;
          return nativeFetch(url, init).then((resp) => {
            if (!resp.ok) badResponses.push(`${resp.status} ${url}`);
            return resp;
          });
        };
        window.ReadableStream = ReadableStream;
        window.TextDecoder = TextDecoder;
        window.TextEncoder = TextEncoder;
        window.AbortController = AbortController;
        window.scrollTo = () => {};
        window.Element.prototype.scrollIntoView = function () {};
        window.HTMLElement.prototype.scrollIntoView = function () {};
        window.addEventListener("error", (e) =>
          pageErrors.push({ type: "window.onerror", msg: String(e.message || e) })
        );
        window.addEventListener("unhandledrejection", (e) =>
          pageErrors.push({
            type: "unhandledrejection",
            msg: String((e.reason && e.reason.message) || e.reason),
          })
        );
      },
    });

    const win = dom.window;
    const doc = win.document;

    const waitFor = async (fn, timeout, label) => {
      const end = Date.now() + timeout;
      while (Date.now() < end) {
        try {
          if (fn()) return true;
        } catch (e) {
          /* 尚未渲染 */
        }
        await sleep(120);
      }
      return check(label, false, "等待超时");
    };

    await waitFor(
      () => doc.querySelectorAll("#ovMetrics .metric").length === 4,
      20000,
      "首屏指标卡渲染完成（4 个）"
    );
    await sleep(800);

    /* ================= 态势总览 ================= */

    const txt = (sel) => {
      const el = doc.querySelector(sel);
      return el ? el.textContent.replace(/\s+/g, " ").trim() : "";
    };
    const metricText = (i) => {
      const m = doc.querySelectorAll("#ovMetrics .metric")[i];
      return m ? m.textContent.replace(/\s+/g, " ").trim() : "";
    };

    check("指标卡数量 = 4", doc.querySelectorAll("#ovMetrics .metric").length === 4, doc.querySelectorAll("#ovMetrics .metric").length);
    check("① 在蹲关键词 = 2（Switch + Kindle）", metricText(0).indexOf("2") >= 0, metricText(0));
    check("② 今日命中 foot 含「累计提醒」", metricText(1).indexOf("累计提醒") >= 0, metricText(1));
    check("③ 距破线最近显示百分比", /%/.test(metricText(2)), metricText(2));
    check("④ 登录 Cookie 显示未配置", metricText(3).indexOf("未配置") >= 0, metricText(3));

    const targetRows = doc.querySelectorAll("#ovTargetList .target");
    check("狙击进度渲染 2 行（= 启用关键词数）", targetRows.length === 2, targetRows.length);
    const targetText = Array.from(targetRows)
      .map((r) => r.textContent.replace(/\s+/g, " ").trim())
      .join(" || ");
    check("进度行含关键词与阈值金额", targetText.indexOf("Switch") >= 0 && targetText.indexOf("¥1,500") >= 0, targetText.slice(0, 130));
    check("进度行已算出「进度 N%」", /进度 \d+%/.test(targetText), (targetText.match(/进度 \d+%/) || ["未找到"])[0]);
    check(
      "命中关键词标为已破线（mock 低价品 < 阈值）",
      doc.querySelectorAll("#ovTargetList .target.hitrow").length === 2,
      `hitrow=${doc.querySelectorAll("#ovTargetList .target.hitrow").length}`
    );
    check("已破线统计文案正确", /已破线 2 \/ 2/.test(txt("#ovTargetsSub")), txt("#ovTargetsSub"));

    const ovHits = doc.querySelectorAll("#ovHitList .hitcard");
    check("今日命中列出记录卡", ovHits.length >= 1, `${ovHits.length} 张`);
    const firstHit = ovHits[0] ? ovHits[0].textContent.replace(/\s+/g, " ").trim() : "";
    check("命中卡含 ¥ 价格", /¥[\d,]+/.test(firstHit), firstHit.slice(0, 120));
    check(
      "命中卡含关键词徽章",
      firstHit.indexOf("Switch") >= 0 || firstHit.indexOf("Kindle") >= 0,
      firstHit.slice(0, 80)
    );
    check("命中卡含「阈值 / 差」差价说明", firstHit.indexOf("阈值") >= 0 && firstHit.indexOf("差") >= 0, firstHit.slice(-90));

    const badge = doc.querySelector("#hitsBadge");
    check("导航角标显示今日命中数", badge && badge.classList.contains("show") && Number(badge.textContent) >= 1, badge ? badge.textContent : "无");
    check("顶栏版本号已填充", /^v\d/.test(txt("#appVersion")), txt("#appVersion"));
    check("顶栏监控脉搏：未运行", txt("#pulseText") === "未运行", txt("#pulseText"));
    check("顶栏 Cookie 灯为未配置态", (doc.querySelector("#healthDot") || { className: "" }).className.indexOf("off") >= 0, (doc.querySelector("#healthDot") || {}).className);
    check(
      "Cookie 未配置时不显示警示条（missing 不打扰）",
      doc.querySelector("#ovCookieAlert").classList.contains("hidden"),
      doc.querySelector("#ovCookieAlert").className
    );

    /* ================= 监控配置 ================= */

    doc.querySelector('.tab[data-view="targets"]').click();
    await sleep(300);
    check("视图切换：监控配置可见", doc.querySelector("#view-targets").classList.contains("active"), doc.querySelector("#view-targets").className);

    const kwCards = doc.querySelectorAll("#kwList .kw-card");
    check("关键词卡 2 张", kwCards.length === 2, kwCards.length);
    check(
      "关键词卡含启用开关与编辑按钮",
      !!kwCards.length &&
        !!kwCards[0].querySelector('input[data-action="kw-toggle"]') &&
        !!kwCards[0].querySelector('button[data-action="kw-edit"]'),
      "结构完整"
    );
    check("参数回填：interval=120", (doc.querySelector("#intervalInput") || {}).value === "120", (doc.querySelector("#intervalInput") || {}).value);
    check("参数回填：fetcher=mock", (doc.querySelector("#fetcherSelect") || {}).value === "mock", (doc.querySelector("#fetcherSelect") || {}).value);
    check(
      "抓取方式下拉含后端全部 3 种（mtop/web/mock）",
      doc.querySelectorAll("#fetcherSelect option").length === 3,
      Array.from(doc.querySelectorAll("#fetcherSelect option")).map((o) => o.value).join(",")
    );
    check("Cookie 状态灯为 off", (doc.querySelector("#cookieLight") || { className: "" }).className.indexOf("off") >= 0, (doc.querySelector("#cookieLight") || {}).className);
    check("Cookie 脱敏位为占位符", txt("#cookieMasked") === "—", txt("#cookieMasked"));
    check("存储路径已回填", txt("#storagePathText").indexOf("xianyu_alert.db") >= 0, txt("#storagePathText"));
    check("系统信息含真实数据目录", txt("#sysInfo").indexOf(dataDir) >= 0, dataDir);

    const interval = doc.querySelector("#intervalInput");
    interval.value = "180";
    interval.dispatchEvent(new win.Event("input", { bubbles: true }));
    await sleep(150);
    check("改参数后出现「未保存」提示条", !doc.querySelector("#dirty-bar").classList.contains("hidden"), doc.querySelector("#dirty-bar").className);
    interval.value = "120";
    interval.dispatchEvent(new win.Event("input", { bubbles: true }));

    /* ================= 命中战果 ================= */

    doc.querySelector('.tab[data-view="hits"]').click();
    await sleep(900);
    check("视图切换：命中战果可见", doc.querySelector("#view-hits").classList.contains("active"), doc.querySelector("#view-hits").className);

    const hitCards = doc.querySelectorAll("#hitsList .hitcard");
    check("命中战果渲染卡片", hitCards.length >= 1, `${hitCards.length} 张`);
    check("命中卡含复选框（可批量）", !!hitCards.length && !!hitCards[0].querySelector("input[data-select]"), "有");
    check(
      "命中卡含打开/已售出/拉黑操作",
      !!hitCards.length &&
        !!hitCards[0].querySelector('button[data-action="sold-one"]') &&
        !!hitCards[0].querySelector('button[data-action="black-one"]'),
      "有"
    );
    check(
      "关键词筛选下拉已填充（全部 + 2 关键词）",
      doc.querySelectorAll("#hitsKeywordFilter option").length === 3,
      Array.from(doc.querySelectorAll("#hitsKeywordFilter option")).map((o) => o.value || "(全部)").join(",")
    );

    const csBtn = doc.querySelector("#hitsCheckShelfBtn");
    check(
      "非 mtop 下「校验在架」按钮被禁用（后端会 400）",
      !!csBtn && csBtn.disabled === true,
      csBtn ? `disabled=${csBtn.disabled} | ${csBtn.title}` : "无按钮"
    );
    // 禁用时提示位必须写明原因（与「可用时不得残留」构成双向断言）
    const csHint = txt("#checkShelfProgress");
    check(
      "禁用状态下提示位说明原因（能力提示双向同步）",
      csHint.indexOf("不支持") >= 0,
      csHint || "空"
    );

    const cb0 = hitCards[0].querySelector("input[data-select]");
    cb0.checked = true;
    cb0.dispatchEvent(new win.Event("change", { bubbles: true }));
    await sleep(120);
    check("勾选后计数更新为「已选 1」", /已选 1/.test(txt("#hitsSelCount")), txt("#hitsSelCount"));

    const search = doc.querySelector("#hitsSearch");
    search.value = "绝不可能匹配的字符串zzz";
    search.dispatchEvent(new win.Event("input", { bubbles: true }));
    await sleep(500);
    check(
      "搜索无结果时渲染空状态",
      doc.querySelectorAll("#hitsList .hitcard").length === 0 && !!doc.querySelector("#hitsList .empty"),
      `卡片=${doc.querySelectorAll("#hitsList .hitcard").length}`
    );
    search.value = "";
    search.dispatchEvent(new win.Event("input", { bubbles: true }));
    await sleep(500);
    check("清空搜索后恢复列表", doc.querySelectorAll("#hitsList .hitcard").length >= 1, `${doc.querySelectorAll("#hitsList .hitcard").length} 张`);

    /* ================= 通知与系统 ================= */

    doc.querySelector('.tab[data-view="system"]').click();
    await sleep(1200);
    check("视图切换：通知与系统可见", doc.querySelector("#view-system").classList.contains("active"), doc.querySelector("#view-system").className);

    const chans = doc.querySelectorAll("#channelList .channel");
    check("渲染 6 类通知通道", chans.length === 6, chans.length);
    const chTypes = Array.from(chans).map((c) => c.dataset.ctype).join(",");
    check("通道顺序与后端 CHANNEL_ORDER 一致", chTypes === "console,serverchan,email,telegram,bark,webhook", chTypes);
    check("console 通道默认启用（enabled 类）", !!chans.length && chans[0].classList.contains("enabled"), chans.length ? chans[0].className : "无");
    check(
      "email 通道渲染 5 个参数字段",
      doc.querySelectorAll('.channel[data-ctype="email"] input[data-field]').length === 5,
      doc.querySelectorAll('.channel[data-ctype="email"] input[data-field]').length
    );
    check(
      "敏感字段用 type=password（sendkey）",
      (doc.querySelector('.channel[data-ctype="serverchan"] input[data-field="sendkey"]') || {}).type === "password",
      (doc.querySelector('.channel[data-ctype="serverchan"] input[data-field="sendkey"]') || {}).type
    );
    check("通道汇总徽章已更新", /已启用 1 个/.test(txt("#chSummary")), txt("#chSummary"));

    const logLines = doc.querySelectorAll("#logBox .log-line");
    check("SSE 实时日志已收到（回放环形缓冲）", logLines.length >= 1, `${logLines.length} 行`);
    check("日志流状态显示已连接", /已连接/.test(txt("#streamStatus")), txt("#streamStatus"));
    check(
      "日志行含级别标记",
      !!logLines.length && /\[(INFO|DEBU|WARN|ERRO|CRIT)\]/.test(logLines[0].textContent),
      logLines.length ? logLines[0].textContent.slice(0, 70) : "无"
    );

    check("系统信息表已填充", doc.querySelectorAll("#sysInfo .kv").length >= 8, doc.querySelectorAll("#sysInfo .kv").length);
    check("系统信息含版本号", /v\d+\.\d+\.\d+/.test(txt("#sysInfo")), (txt("#sysInfo").match(/v\d+\.\d+\.\d+/) || ["无"])[0]);
    check("黑名单区渲染空状态", !!doc.querySelector("#blackList .empty"), txt("#blackList").slice(0, 40));

    /* ================= 命令面板 ================= */

    win.XY.Cmdk.show();
    await sleep(250);
    check("命令面板可打开", !!doc.querySelector("#cmdk-root .cmdk"), "已渲染");
    check("命令面板列出默认命令", doc.querySelectorAll("#cmdk-root .cmdk-item").length >= 5, doc.querySelectorAll("#cmdk-root .cmdk-item").length);
    const cmdkInput = doc.querySelector("#cmdkInput");
    cmdkInput.value = "命中";
    cmdkInput.dispatchEvent(new win.Event("input", { bubbles: true }));
    await sleep(250);
    check("命令面板支持输入过滤", doc.querySelectorAll("#cmdk-root .cmdk-item").length >= 1, doc.querySelectorAll("#cmdk-root .cmdk-item").length);
    win.XY.Cmdk.close();
    await sleep(120);
    check("命令面板可关闭", !doc.querySelector("#cmdk-root .cmdk"), "已关闭");

    /* ================= 运行质量 ================= */

    check("所有 /api 请求均为 2xx", badResponses.length === 0, badResponses.slice(0, 5).join(" | ") || "全部成功");
    const realErrors = pageErrors.filter((e) => e.type !== "css");
    check(
      "无未捕获脚本错误",
      realErrors.length === 0,
      realErrors.slice(0, 4).map((e) => `[${e.type}] ${e.msg.slice(0, 220)}`).join(" | ") || "无"
    );
    check("无 console.error 输出", consoleErrors.length === 0, consoleErrors.slice(0, 3).join(" | ") || "无");

    /* ================= 输出 ================= */

    const width = Math.max(...ROWS.map((r) => r.name.length));
    console.log("\n" + "=".repeat(80));
    console.log("前端 × 真实后端 DOM 端到端验证");
    console.log("=".repeat(80));
    for (const r of ROWS) console.log(`  ${r.ok ? "✅" : "❌"} ${r.name.padEnd(width)}  ${r.detail}`);
    const bad = ROWS.filter((r) => !r.ok);
    console.log("-".repeat(80));
    console.log(`共 ${ROWS.length} 项：通过 ${ROWS.length - bad.length}，失败 ${bad.length}`);
    if (pageErrors.some((e) => e.type === "css")) {
      console.log(`ℹ️  jsdom 报了 ${pageErrors.filter((e) => e.type === "css").length} 条 CSS 解析告警（backdrop-filter 等现代语法），非页面问题`);
    }
    if (bad.length) {
      console.log("\n失败明细：");
      bad.forEach((r) => console.log(`  - ${r.name}：${r.detail}`));
    }

    dom.window.close();
    cleanup();
    await sleep(300);
    process.exit(bad.length ? 1 : 0);
  } catch (e) {
    console.error("验证过程异常：", e);
    if (serverOut) console.error("后端输出：\n" + serverOut.slice(0, 2000));
    cleanup();
    process.exit(1);
  }
})();
