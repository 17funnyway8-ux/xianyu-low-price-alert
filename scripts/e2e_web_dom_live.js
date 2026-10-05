#!/usr/bin/env node
/* ===========================================================
 * scripts/e2e_web_dom_live.js —— 对【已部署实例】跑 DOM 端到端（只读）
 *
 * 与 e2e_web_dom.js 的分工：
 *   e2e_web_dom.js    ：自己拉临时 uvicorn + mock 抓取器，
 *                       可以随便写数据（run_once / clear），断言也很具体；
 *   e2e_web_dom_live.js（本脚本）：打线上地址，**只读**。
 *                       生产实例上挂着真实 Cookie 与真实关键词，
 *                       绝不能因为一次测试而改动配置、写记录、触发真实抓取。
 *
 * 因此本脚本内建一条硬约束：**任何非 GET 请求直接判失败**。
 * 跑完即可证明「这次验收对生产零写入」。
 *
 * 用法：
 *   NODE_PATH=<jsdom> node scripts/e2e_web_dom_live.js
 *   NODE_PATH=<jsdom> node scripts/e2e_web_dom_live.js --base http://10.0.0.26:8080
 * 退出码：0 = 全绿；1 = 有失败；2 = 缺 jsdom
 * =========================================================== */
"use strict";

const path = require("path");
const net = require("net");
const http = require("http");

let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require("jsdom"));
} catch (e) {
  console.error("缺少 jsdom。请用 NODE_PATH 指向已装目录：npm i jsdom");
  process.exit(2);
}

const argv = process.argv.slice(2);
const argOf = (name, def) => {
  const i = argv.indexOf(name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : def;
};
const BASE = argOf("--base", "http://127.0.0.1:8080").replace(/\/$/, "");

const ROWS = [];
function check(name, cond, detail) {
  ROWS.push({ name, ok: !!cond, detail: detail === undefined ? "" : String(detail) });
  return !!cond;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* 直连探测：确认目标可达（比 fetch 更早给出明确错误） */
function probe(base) {
  return new Promise((resolve) => {
    const u = new URL(base + "/healthz");
    const req = http.get(
      { host: u.hostname, port: u.port || 80, path: u.pathname, timeout: 5000 },
      (res) => {
        let b = "";
        res.on("data", (d) => (b += d));
        res.on("end", () => resolve({ status: res.statusCode, body: b }));
      }
    );
    req.on("error", (e) => resolve({ error: e.message }));
    req.on("timeout", () => {
      req.destroy();
      resolve({ error: "timeout" });
    });
  });
}

(async function main() {
  console.log("=".repeat(72));
  console.log(`闲鱼低价提醒工具 · 前端 DOM 验收（只读）  →  ${BASE}`);
  console.log("=".repeat(72));

  const hz = await probe(BASE);
  if (hz.error || hz.status !== 200) {
    console.error(`\n❌ 目标不可达：${hz.error || "HTTP " + hz.status}`);
    process.exit(2);
  }
  let health = {};
  try {
    health = JSON.parse(hz.body);
  } catch (e) {
    /* ignore */
  }
  console.log(`目标健康：version=${health.version} monitor=${health.monitor_running}\n`);

  /* -------- 记录所有请求，用于「零写入」证明 -------- */
  const requests = [];
  const nonGet = [];
  const badResponses = [];
  const pageErrors = [];
  const consoleErrors = [];
  const nativeFetch = fetch;

  const vc = new VirtualConsole();
  vc.on("jsdomError", (err) => {
    const msg = String((err && err.message) || err);
    // 页面里的 CSS 解析宽容度与浏览器不同，这类噪声不计入
    if (/Could not parse CSS/i.test(msg)) return;
    pageErrors.push(msg);
  });
  vc.on("error", (...a) => consoleErrors.push(a.map(String).join(" ")));

  let dom;
  try {
    dom = await JSDOM.fromURL(BASE + "/", {
      runScripts: "dangerously",
      resources: "usable",
      pretendToBeVisual: true,
      virtualConsole: vc,
      beforeParse(window) {
        window.fetch = (input, init) => {
          const raw = typeof input === "string" ? input : (input && input.url) || String(input);
          const abs = new URL(raw, BASE).toString();
          const method = ((init && init.method) || "GET").toUpperCase();
          requests.push(method + " " + abs);
          if (method !== "GET") nonGet.push(method + " " + abs);
          return nativeFetch(abs, init).then((resp) => {
            if (!resp.ok) badResponses.push(`${resp.status} ${method} ${abs}`);
            return resp;
          });
        };
        // jsdom 不提供这些宿主能力，必须在页面脚本执行前补上
        window.ReadableStream = ReadableStream;
        window.TextDecoder = TextDecoder;
        window.TextEncoder = TextEncoder;
        window.AbortController = AbortController;
        window.scrollTo = () => {};
        window.Element.prototype.scrollIntoView = function () {};
        window.HTMLElement.prototype.scrollIntoView = function () {};
        window.addEventListener("error", (e) => pageErrors.push(String(e.message || e)));
        window.addEventListener("unhandledrejection", (e) =>
          pageErrors.push(String((e.reason && e.reason.message) || e.reason))
        );
      },
    });
  } catch (e) {
    console.error("\n❌ 装载页面失败：" + e.message);
    process.exit(1);
  }

  const win = dom.window;
  const doc = win.document;
  const txt = (sel) => {
    const el = doc.querySelector(sel);
    return el ? el.textContent.replace(/\s+/g, " ").trim() : "";
  };

  const waitFor = async (fn, timeout, label) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      try {
        if (fn()) return true;
      } catch (e) {
        /* 还没渲染 */
      }
      await sleep(120);
    }
    return check(label, false, "等待超时");
  };

  /* ================= 1. 首屏 ================= */
  console.log("[1] 首屏与运行时报错");
  await waitFor(
    () => doc.querySelectorAll("#ovMetrics .metric").length === 4,
    25000,
    "首屏 4 张指标卡渲染完成"
  );
  await sleep(1000);

  check("首屏指标卡 = 4", doc.querySelectorAll("#ovMetrics .metric").length === 4,
    doc.querySelectorAll("#ovMetrics .metric").length);
  check("存在四视图容器 / 四个导航页签",
    doc.querySelectorAll("section.view").length === 4 &&
      doc.querySelectorAll("nav .tab").length === 4,
    `${doc.querySelectorAll("section.view").length} view / ${doc.querySelectorAll("nav .tab").length} tab`);
  check("默认视图为态势总览", !!doc.querySelector("#view-overview.view.active"),
    (doc.querySelector("section.view.active") || {}).id);
  check("无未捕获脚本错误", pageErrors.length === 0,
    pageErrors.slice(0, 3).join(" | ") || "无");
  check("无 console.error 输出", consoleErrors.length === 0,
    consoleErrors.slice(0, 3).join(" | ") || "无");

  /* ================= 2. 顶栏真实数据 ================= */
  console.log("\n[2] 顶栏（真实后端数据）");
  const hdot = doc.querySelector("#healthDot");
  check("Cookie 健康灯带状态类（dot + on/off/warn/err）",
    !!hdot && /^dot (on|off|warn|err)$/.test(hdot.className),
    hdot ? hdot.className : "缺失");
  check("顶栏 Cookie 文案已填充", txt("#healthText").length > 0, txt("#healthText"));
  check("版本号来自后端", /^v?\d+\.\d+/.test(txt("#appVersion")), txt("#appVersion"));
  const pulse = doc.querySelector("#pulseBtn");
  check("运行脉冲按钮存在且有提示语", !!pulse && (pulse.title || "").length > 0,
    pulse ? pulse.title : "缺失");

  /* ================= 3. 四个视图逐个切换 ================= */
  console.log("\n[3] 四视图切换与渲染");
  const views = ["overview", "targets", "hits", "system"];
  for (const v of views) {
    const btn = doc.querySelector(`nav .tab[data-view="${v}"]`);
    if (!btn) {
      check(`视图 ${v} 有导航按钮`, false, "未找到");
      continue;
    }
    btn.click();
    await sleep(700);
    const sec = doc.querySelector(`#view-${v}`);
    check(`切到 ${v}：容器激活`, !!sec && sec.classList.contains("active"),
      sec ? sec.className : "缺失");
    check(`切到 ${v}：内容非空`, !!sec && sec.textContent.replace(/\s+/g, "").length > 20,
      sec ? sec.textContent.replace(/\s+/g, "").length + " 字" : "0");
    check(`切到 ${v}：导航高亮跟上`, btn.classList.contains("active"), btn.className);
  }

  /* ================= 4. 各视图的真实数据渲染 ================= */
  console.log("\n[4] 真实数据渲染");

  const metricText = (i) => {
    const m = doc.querySelectorAll("#ovMetrics .metric")[i];
    return m ? m.textContent.replace(/\s+/g, " ").trim() : "";
  };
  check("① 在蹲关键词卡有数字", /\d/.test(metricText(0)), metricText(0).slice(0, 60));
  check("② 今日命中卡有数字", /\d/.test(metricText(1)), metricText(1).slice(0, 60));
  check("④ 登录 Cookie 卡已渲染", metricText(3).length > 0, metricText(3).slice(0, 60));

  const targetRows = doc.querySelectorAll("#ovTargetList .target");
  check("狙击进度行已渲染（= 启用关键词数）", targetRows.length > 0, `${targetRows.length} 行`);
  if (targetRows.length) {
    const tt = Array.from(targetRows).map((r) => r.textContent.replace(/\s+/g, " ")).join(" || ");
    check("进度行含关键词与阈值金额", /¥|￥/.test(tt), tt.slice(0, 120));
  }

  // 监控配置页：Cookie 状态灯（本轮修复的回归点）
  const cl = doc.querySelector("#cookieLight");
  check("★ 配置页 Cookie 状态灯带颜色类（回归点）",
    !!cl && /^status-light (on|off|warn|err)$/.test(cl.className),
    cl ? cl.className : "缺失");
  check("配置页 Cookie 文案与顶栏一致来源",
    txt("#cookieStateText").length > 0, txt("#cookieStateText"));
  const fetcher = doc.querySelector("#fetcherSelect");
  check("抓取方式下拉已回填", !!fetcher && fetcher.value.length > 0,
    fetcher ? fetcher.value : "缺失");
  // 生产实例是 mtop，与 mock 相反：校验在架应当可用
  const csBtn = doc.querySelector("#hitsCheckShelfBtn");
  if (fetcher && fetcher.value === "mtop") {
    check("mtop 下「校验在架」可用（与 mock 行为相反的正确分支）",
      !!csBtn && !csBtn.disabled, csBtn ? `disabled=${csBtn.disabled}` : "缺失");
    // 回归点：按钮启用时，提示位不得残留「不支持」错话
    // （首屏 config 未加载时写过一次，恢复可用后必须撤回）
    check("★ 启用状态下提示位无「不支持」残留（回归点）",
      txt("#checkShelfProgress").indexOf("不支持") < 0,
      txt("#checkShelfProgress") || "空");
  } else {
    check("非 mtop 下「校验在架」被禁用（防后端 400）",
      !!csBtn && csBtn.disabled, csBtn ? `disabled=${csBtn.disabled}` : "缺失");
    check("禁用状态下提示位说明原因",
      txt("#checkShelfProgress").indexOf("不支持") >= 0, txt("#checkShelfProgress"));
  }
  check("存储路径已回填", txt("#storagePathText").length > 0, txt("#storagePathText"));

  // 通知与系统页
  doc.querySelector('nav .tab[data-view="system"]').click();
  await sleep(900);
  const channels = doc.querySelectorAll("#channelList .channel");
  check("渲染 6 类通知通道", channels.length === 6, channels.length);
  const order = Array.from(channels)
    .map((c) => (c.getAttribute("data-ctype") || "").trim())
    .filter(Boolean)
    .join(",");
  check("通道顺序与后端 CHANNEL_ORDER 一致",
    order === "console,serverchan,email,telegram,bark,webhook", order || "未取到 data-ctype");
  check("系统信息表已填充", txt("#sysInfo").length > 0, txt("#sysInfo").slice(0, 70));

  // 命中战果页：真实记录
  doc.querySelector('nav .tab[data-view="hits"]').click();
  await sleep(900);
  const hitCards = doc.querySelectorAll("#hitsList .hitcard");
  check("命中战果列表已渲染（真实记录）", hitCards.length > 0,
    `${hitCards.length} 张卡片`);
  check("搜索框存在", !!doc.querySelector("#hitsSearch"));
  check("关键词筛选下拉存在", !!doc.querySelector("#hitsKeywordFilter"));

  /* ================= 5. SSE 日志流 ================= */
  console.log("\n[5] SSE 实时日志");
  doc.querySelector('nav .tab[data-view="system"]').click();
  // 等流建立：streamStatus 从「未连接」走到「已连接」
  await waitFor(() => txt("#streamStatus") === "已连接", 15000, "SSE 流状态显示已连接");
  await sleep(1200);
  check("SSE 流状态 = 已连接", txt("#streamStatus") === "已连接", txt("#streamStatus"));
  const logBox = doc.querySelector("#logBox");
  check("日志容器存在", !!logBox, logBox ? logBox.id : "缺失");
  const lines = logBox ? logBox.querySelectorAll(".log-line").length : 0;
  const count = txt("#logCount");
  // #logCount 形如「3 / 3 条」；有值即证明 renderLogs 跑过（未卡在空占位）
  check("日志渲染已生效（#logCount 已填充）", /^\d+ \/ \d+ 条$/.test(count), count || "空");
  if (lines === 0) {
    console.log("      （本次未取到日志行：容器环形缓冲可能为空，非前端问题）");
  } else {
    const first = logBox.querySelector(".log-line").textContent.replace(/\s+/g, " ").trim();
    console.log("      首行日志：" + first.slice(0, 80));
    check("日志行含级别标记", /\[[A-Za-z]{3,8}\]/.test(first), first.slice(0, 40));
  }

  /* ================= 6. 命令面板 ================= */
  console.log("\n[6] 命令面板");
  const cmdkBtn = doc.querySelector("#cmdkBtn");
  const cmdkRoot = doc.querySelector("#cmdk-root");
  if (cmdkBtn && cmdkRoot) {
    cmdkBtn.click();
    await sleep(600);
    const items = doc.querySelectorAll("#cmdk-root .cmdk-item");
    check("命令面板可打开并列出命令", items.length > 0, `${items.length} 项`);
    const mask = doc.querySelector("#cmdkMask");
    check("命令面板有遮罩层", !!mask, mask ? mask.className : "缺失");
    const input = doc.querySelector("#cmdkInput");
    check("命令面板有输入框", !!input, input ? input.id : "缺失");
    // Esc 关闭（cmdk.js 监听 document keydown）
    win.document.dispatchEvent(
      new win.KeyboardEvent("keydown", { key: "Escape", bubbles: true })
    );
    await sleep(500);
    check("Esc 可关闭命令面板", cmdkRoot.innerHTML.trim() === "",
      cmdkRoot.innerHTML === "" ? "已清空" : cmdkRoot.innerHTML.slice(0, 40));
  } else {
    check("命令面板入口与根节点存在", false,
      cmdkBtn ? "缺 #cmdk-root" : "缺 #cmdkBtn");
  }

  /* ================= 7. 请求卫生 ================= */
  console.log("\n[7] 请求卫生");
  const uniq = Array.from(new Set(requests));
  check("前端确实发出了 /api 请求", requests.some((r) => r.includes("/api/")),
    `${uniq.length} 个不同请求`);
  check("★ 全程只发 GET（对生产零写入）", nonGet.length === 0,
    nonGet.length ? "违规：" + nonGet.slice(0, 5).join("; ") : "0 次写请求");
  check("所有请求均 2xx", badResponses.length === 0,
    badResponses.slice(0, 5).join("; ") || "全部成功");
  const apiPaths = Array.from(
    new Set(requests.filter((r) => r.includes("/api/")).map((r) => r.split(" ")[1].replace(BASE, "").split("?")[0]))
  ).sort();
  console.log("      实际命中接口：" + apiPaths.join(", "));

  /* ================= 汇总 ================= */
  const pass = ROWS.filter((r) => r.ok).length;
  const fail = ROWS.length - pass;
  console.log("\n" + "-".repeat(72));
  // 逐项列出（验收证据，可直接留档）
  ROWS.forEach((r) => {
    console.log(
      (r.ok ? "  ✅ " : "  ❌ ") + r.name + (r.detail ? "\t\t" + r.detail : "")
    );
  });
  console.log("-".repeat(72));
  console.log(`共 ${ROWS.length} 项：通过 ${pass}，失败 ${fail}`);
  if (fail) {
    console.log("\n失败明细：");
    ROWS.filter((r) => !r.ok).forEach((r) => console.log(`  - ${r.name}  ${r.detail}`));
  }
  try {
    dom.window.close();
  } catch (e) {
    /* ignore */
  }
  process.exit(fail ? 1 : 0);
})();
