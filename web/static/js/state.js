/* ===========================================================
 * 闲鱼低价提醒工具 —— state.js（全局共享状态 + 派生逻辑）
 * 挂载到 window.XY.State。
 *
 * 单一真源约定：所有「由后端字段派生出的前端字段」都集中在 deco()，
 * 视图层不得自行猜测字段名 —— 后端字段名以 web/api.py + storage.py 为准。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.State = (function () {
  "use strict";
  const U = window.XY.Util;

  /* ------------------------------------------------------------------ #
   * 通道元数据（与 xianyu_alert/gui.py 的 CHANNEL_ORDER / CHANNEL_LABELS
   * / CHANNEL_FIELDS 逐字对齐；改动必须同步后端）
   * 字段元组：[key, label, isSecret, default]
   * ------------------------------------------------------------------ */
  const CHANNEL_ORDER = ["console", "serverchan", "email", "telegram", "bark", "webhook"];

  const CHANNEL_LABELS = {
    console: "控制台（打印到日志区，永远可用）",
    serverchan: "Server酱（微信推送）",
    email: "邮件（SMTP）",
    telegram: "Telegram Bot",
    bark: "Bark（iOS 推送）",
    webhook: "企业微信机器人（Webhook）",
  };

  const CHANNEL_TAGS = {
    console: "日志",
    serverchan: "微信",
    email: "SMTP",
    telegram: "TG",
    bark: "iOS",
    webhook: "企微",
  };

  const CHANNEL_FIELDS = {
    console: [],
    serverchan: [["sendkey", "SendKey", true, ""]],
    email: [
      ["smtp_host", "SMTP 服务器", false, "smtp.qq.com"],
      ["smtp_port", "端口（465=SSL / 587=TLS）", false, "465"],
      ["username", "账号（同时作为发件人）", false, ""],
      ["password", "密码 / 授权码", true, ""],
      ["to", "收件人（多个用英文逗号分隔）", false, ""],
    ],
    telegram: [
      ["bot_token", "Bot Token", true, ""],
      ["chat_id", "Chat ID", false, ""],
    ],
    bark: [["url", "Bark URL（形如 https://api.day.app/YourKey/）", false, "https://api.day.app/"]],
    webhook: [["url", "Webhook URL（企业微信群机器人地址）", false, ""]],
  };

  /* ------------------------------------------------------------------ #
   * Cookie 健康状态 → 视觉映射
   * state 取值与 xianyu_alert/cookie.py detect_cookie_health 对齐：
   *   ok / expiring / expired / no_token / missing / invalid_encrypt
   * ------------------------------------------------------------------ */
  const COOKIE_STATES = {
    ok: { dot: "on", brief: "Cookie 正常" },
    expiring: { dot: "warn", brief: "Cookie 即将过期" },
    expired: { dot: "err", brief: "Cookie 已过期" },
    no_token: { dot: "err", brief: "Cookie 缺 _m_h5_tk" },
    missing: { dot: "off", brief: "Cookie 未配置" },
    invalid_encrypt: { dot: "err", brief: "Cookie 无法解密" },
  };

  /**
   * dot → 指标卡色调。
   * 全站状态色只用 on / off / warn / err 四个词，
   * 避免再开第二套词汇（历史上曾是 dot=on/off + level=ok/"")。
   */
  function dotTone(dot) {
    if (dot === "err") return "err";
    if (dot === "on") return "up";
    return "";
  }

  /* ------------------------------------------------------------------ #
   * 全局状态
   * ------------------------------------------------------------------ */
  const S = {
    /** GET /api/config 返回的表单 */
    config: null,
    /** 配置基线快照（用于「丢弃改动」与脏检查） */
    configBase: null,
    /** GET /api/monitor/status */
    status: null,
    /** GET /api/records 的 records[] */
    records: [],
    /** GET /api/records 的 total */
    recordsTotal: 0,
    /**
     * 上一次 loadRecords 的口径标识（反映 includeSold）。
     * 口径变化会让「已售出」记录突然出现在列表里，若不重置 knownIds，
     * app.detectNewHits 会把它们误判成新命中并弹「发现低价」。
     */
    recordsScope: null,
    /** GET /api/blacklist 的 items[] */
    blacklist: [],
    /** GET /api/cookie/pool */
    pool: null,
    /** GET /healthz */
    health: null,
    /** GET /api/records/check_shelf/status */
    checkShelf: null,

    /** 当前视图 */
    view: "overview",
    /** 配置是否有未保存改动 */
    dirty: false,

    /** 记录筛选（客户端） */
    filters: {
      q: "",
      keyword: "",
      todayOnly: false,
      includeSold: false,
      sort: "time",
      order: "desc",
    },
    /** 批量勾选：product_id 集合 */
    selected: {},

    /** 日志 */
    logs: [],
    logFilter: "",
    logFontSize: 11.5,
    streamState: "idle",

    /** 动画：本轮新命中的 product_id（渲染后清空） */
    freshIds: {},
    /** 上一轮已知的 product_id（用于 diff 出新命中） */
    knownIds: null,

    /** 顶部状态轮询句柄 */
    statusTimer: null,
    /** 本地倒计时剩余秒（由 next_round_in 起步，逐秒递减） */
    countdown: null,
    /** 校验在架轮询句柄 */
    checkShelfTimer: null,
  };

  /* ------------------------------------------------------------------ #
   * 派生：配置侧
   * ------------------------------------------------------------------ */

  /** 关键词数组（容错）。 */
  function keywords() {
    return (S.config && S.config.keywords) || [];
  }

  /** 关键词 → max_price 映射。 */
  function thresholdMap() {
    const m = {};
    keywords().forEach((k) => {
      const v = Number(k.max_price);
      if (isFinite(v) && v > 0) m[k.keyword] = v;
    });
    return m;
  }

  /** 某关键词的阈值；无配置返回 null（切不可当 0 处理）。 */
  function thresholdOf(kw) {
    const v = thresholdMap()[kw];
    return v === undefined ? null : v;
  }

  /* ------------------------------------------------------------------ #
   * 派生：记录侧
   *
   * 后端 /api/records 返回字段（storage.list_notified → SELECT * ）：
   *   keyword, product_id, title, price, url, publish_time,
   *   first_seen, last_seen, notified, sold_out, sold_at, sold_reason
   * 外加 api.py 补的两个别名：time(=last_seen)、publish(=publish_time)
   * price 是「最近一次观测到的价格」（storage 的 upsert 会刷 price/last_seen）
   * ------------------------------------------------------------------ */

  /**
   * 给一条原始记录补上前端派生字段。
   * @param {object} r 后端原始记录
   */
  function deco(r) {
    const time = U.normTime(r.time || r.last_seen || "");
    const publish = U.normTime(r.publish || r.publish_time || "");
    const price = Number(r.price);
    const thresh = thresholdOf(r.keyword);
    const hasThresh = thresh !== null && isFinite(thresh) && thresh > 0;
    const below = hasThresh ? thresh - price : null;
    return Object.assign({}, r, {
      price: isFinite(price) ? price : 0,
      time: time,
      publish: publish,
      day: U.dayOf(time),
      thresh: hasThresh ? thresh : null,
      below: below,
      /** 命中深度：现价比阈值低多少（比例） */
      depth: hasThresh && price > 0 ? (thresh - price) / thresh : null,
      isToday: U.dayOf(time) === U.todayStr(),
      /** 是否已跌破阈值（price ≤ 阈值） */
      isHit: hasThresh ? price <= thresh : true,
      sold: !!r.sold_out,
      url: r.url || "",
    });
  }

  /** 全部记录（已派生）。 */
  function recs() {
    return (S.records || []).map(deco);
  }

  /** 按当前筛选条件过滤后的记录（仍走服务端排序，仅做客户端过滤）。 */
  function filteredRecs() {
    const f = S.filters;
    const q = f.q.trim().toLowerCase();
    return recs().filter((r) => {
      if (f.todayOnly && !r.isToday) return false;
      if (f.keyword && r.keyword !== f.keyword) return false;
      if (q && String(r.title).toLowerCase().indexOf(q) < 0) return false;
      return true;
    });
  }

  /** 今日命中（未售出）。 */
  function todayHits() {
    return recs()
      .filter((r) => r.isToday && !r.sold)
      .sort((a, b) => (a.time < b.time ? 1 : a.time > b.time ? -1 : 0));
  }

  /**
   * 某关键词下的「现价」代理：未售出记录里的最低价。
   * 说明：/api/records 只返回已命中(notified=1)的商品，因此
   * 未命中关键词拿不到任何价格 —— 此时返回 null，绝不编造。
   */
  function currentPrice(kw) {
    const list = recs().filter((r) => r.keyword === kw && !r.sold);
    if (!list.length) return null;
    return Math.min.apply(null, list.map((r) => r.price));
  }

  /**
   * 狙击进度：阈值 ÷ 现价。
   * ≥1 表示现价已低于阈值（破线）；<1 表示还差阈值-现价。
   * 返回 {price, ratio, gap, hit} 或 null（无数据）。
   */
  function progressOf(kw) {
    const thresh = thresholdOf(kw);
    const price = currentPrice(kw);
    if (thresh === null || price === null || price <= 0) return null;
    const ratio = thresh / price;
    return { price: price, thresh: thresh, ratio: ratio, gap: price - thresh, hit: ratio >= 1 };
  }

  /** 「距破线最近」的关键词（ratio 最大，已破线时 >1）与「已破线」数量。 */
  function closestToLine() {
    let best = null;
    let hits = 0;
    keywords()
      .filter((k) => k.enabled !== false)
      .forEach((k) => {
        const p = progressOf(k.keyword);
        if (!p) return;
        if (p.hit) hits += 1;
        if (!best || p.ratio > best.ratio) best = { keyword: k.keyword, ratio: p.ratio, price: p.price, thresh: p.thresh };
      });
    return { best: best, hits: hits };
  }

  /** 已知关键词集合（用于记录页的下拉）。 */
  function knownKeywords() {
    const set = {};
    keywords().forEach((k) => (set[k.keyword] = 1));
    recs().forEach((r) => (set[r.keyword] = 1));
    return Object.keys(set).sort();
  }

  function cookieHealth() {
    const cfg = S.config;
    if (cfg && cfg.cookie_health) return cfg.cookie_health;
    return { state: "missing", text: "未配置 Cookie" };
  }

  function cookieMeta() {
    const st = cookieHealth().state || "missing";
    return COOKIE_STATES[st] || COOKIE_STATES.missing;
  }

  /* ------------------------------------------------------------------ #
   * 脏检查
   * ------------------------------------------------------------------ */
  function snapshotConfig() {
    try {
      S.configBase = JSON.parse(JSON.stringify(S.config));
    } catch (e) {
      S.configBase = null;
    }
  }

  function markDirty() {
    if (S.dirty) return;
    S.dirty = true;
    window.dispatchEvent(new CustomEvent("xy:dirty"));
  }

  function clearDirty() {
    S.dirty = false;
    window.dispatchEvent(new CustomEvent("xy:dirty"));
  }

  function restoreConfig() {
    if (S.configBase) S.config = JSON.parse(JSON.stringify(S.configBase));
    clearDirty();
  }

  return {
    s: S,
    CHANNEL_ORDER: CHANNEL_ORDER,
    CHANNEL_LABELS: CHANNEL_LABELS,
    CHANNEL_TAGS: CHANNEL_TAGS,
    CHANNEL_FIELDS: CHANNEL_FIELDS,
    COOKIE_STATES: COOKIE_STATES,

    keywords: keywords,
    thresholdMap: thresholdMap,
    thresholdOf: thresholdOf,

    deco: deco,
    recs: recs,
    filteredRecs: filteredRecs,
    todayHits: todayHits,
    currentPrice: currentPrice,
    progressOf: progressOf,
    closestToLine: closestToLine,
    knownKeywords: knownKeywords,

    cookieHealth: cookieHealth,
    cookieMeta: cookieMeta,
    dotTone: dotTone,

    snapshotConfig: snapshotConfig,
    markDirty: markDirty,
    clearDirty: clearDirty,
    restoreConfig: restoreConfig,
  };
})();
