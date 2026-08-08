/* ===========================================================
 * 闲鱼低价提醒工具 —— state.js（全局共享状态）
 * 挂载到 window.XY.State。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.State = {
  // GET /api/config 返回的表单（含 keywords/channels/监测参数/Cookie 脱敏）
  config: null,

  // 通知通道顺序与文案
  channelOrder: ["console", "serverchan", "email", "telegram", "bark", "webhook"],
  channelLabels: {
    console: "控制台（打印到容器日志，永远可用）",
    serverchan: "Server酱（微信推送）",
    email: "邮件（SMTP）",
    telegram: "Telegram Bot",
    bark: "Bark（iOS 推送）",
    webhook: "企业微信机器人（Webhook）",
  },
  channelFields: {
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
  },

  // 记录表排序
  recordSort: "time",
  recordOrder: "desc",

  // 日志工具条（P2-12）
  logFilter: "",
  logFontSize: 12,

  // 校验在架进度轮询句柄
  checkShelfTimer: null,
};
