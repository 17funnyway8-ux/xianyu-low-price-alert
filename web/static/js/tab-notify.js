/* ===========================================================
 * 闲鱼低价提醒工具 —— tab-notify.js（Tab2 通知设置）
 * 挂载到 window.XY.TabNotify。
 *
 * 职责（P2-13/P2-14）：
 *   - 6 通道卡片渲染（勾选/参数表单）
 *   - 测试发送（按钮 loading 态 + 结果回显 + 校验反馈统一）
 * =========================================================== */
window.XY = window.XY || {};
window.XY.TabNotify = (function () {
  "use strict";
  const U = window.XY.Util;
  const Api = window.XY.Api;
  const State = window.XY.State;

  function renderChannels(data) {
    const channels = data.channels || {};
    const list = U.$("#channel-list");
    if (!list) return;
    if (!Object.keys(channels).length) {
      list.innerHTML = '<div class="empty-card">暂无通道配置</div>';
      return;
    }
    list.innerHTML = State.channelOrder
      .map((ctype) => {
        const ch = channels[ctype] || { enabled: false, options: {} };
        const options = ch.options || {};
        const fields = State.channelFields[ctype] || [];
        const fieldHtml = fields
          .map(([key, label, isSecret, def]) => {
            const value = options[key] !== undefined ? options[key] : def;
            return `<label>${U.escapeHtml(label)}
              <input type="${isSecret ? "password" : "text"}" data-ctype="${ctype}" data-field="${key}"
                value="${U.escapeHtml(value)}" autocomplete="off"></label>`;
          })
          .join("");
        return `
        <div class="channel-card ${ch.enabled ? "enabled" : ""}" data-ctype="${ctype}">
          <div class="channel-head">
            <span class="channel-name">${U.escapeHtml(State.channelLabels[ctype] || ctype)}</span>
            <label class="switch">
              <input type="checkbox" data-ctype="${ctype}" data-action="channel-toggle"
                ${ch.enabled ? "checked" : ""}>
              <span class="slider"></span>
            </label>
          </div>
          <div class="channel-fields">${fieldHtml}</div>
          <div class="channel-actions">
            <button class="btn small" data-ctype="${ctype}" data-action="channel-test" type="button">测试发送</button>
            <span class="channel-test-result" data-ctype="${ctype}"></span>
          </div>
        </div>`;
      })
      .join("");
  }

  function channelOptions(ctype) {
    const options = {};
    U.$$(`.channel-card[data-ctype="${ctype}"] input[data-field]`).forEach((input) => {
      options[input.dataset.field] = input.value.trim();
    });
    return options;
  }

  async function testChannel(ctype, btn) {
    const resultEl = document.querySelector(`.channel-test-result[data-ctype="${ctype}"]`);
    U.setBtnLoading(btn, true, "发送中…");
    if (resultEl) resultEl.textContent = "发送中…";
    try {
      await Api.api("/api/notify/test", {
        method: "POST",
        body: { channel_type: ctype, options: channelOptions(ctype) },
      });
      if (resultEl) resultEl.textContent = "✓ 已发送";
      U.toast("测试消息已发送");
    } catch (err) {
      if (resultEl) resultEl.textContent = "✗ " + err.message;
      U.toast("测试发送失败：" + err.message, true);
    } finally {
      U.setBtnLoading(btn, false, "测试发送");
    }
  }

  function init() {
    U.$("#channel-list").addEventListener("change", (e) => {
      const input = e.target.closest('input[data-action="channel-toggle"]');
      if (!input) return;
      const card = input.closest(".channel-card");
      card.classList.toggle("enabled", input.checked);
      const ctype = input.dataset.ctype;
      State.config.channels = State.config.channels || {};
      State.config.channels[ctype] = State.config.channels[ctype] || { enabled: false, options: {} };
      State.config.channels[ctype].enabled = input.checked;
    });

    U.$("#channel-list").addEventListener("input", (e) => {
      const input = e.target.closest("input[data-field]");
      if (!input) return;
      const ctype = input.dataset.ctype;
      State.config.channels = State.config.channels || {};
      State.config.channels[ctype] = State.config.channels[ctype] || { enabled: false, options: {} };
      State.config.channels[ctype].options = channelOptions(ctype);
    });

    U.$("#channel-list").addEventListener("click", (e) => {
      const btn = e.target.closest('button[data-action="channel-test"]');
      if (!btn) return;
      testChannel(btn.dataset.ctype, btn);
    });
  }

  return {
    init: init,
    renderChannels: renderChannels,
    testChannel: testChannel,
  };
})();
