/* ===========================================================
 * 闲鱼低价提醒工具 —— modal.js（通用弹窗组件）
 * 挂载到 window.XY.Modal。
 *
 * 三个工厂（对齐设计 §3.2）：
 *   - open({title, bodyHtml, width, onMount, onClose})   通用弹窗
 *   - confirm({title, message, danger, confirmText, onConfirm})  二次确认
 *   - prompt({title, fields, validate, onSave})           表单弹窗
 *
 * P2 实例：Cookie 池管理 / 黑名单管理 / 过滤词 / 预置词 / 关于 / 关键词编辑。
 * =========================================================== */
window.XY = window.XY || {};
window.XY.Modal = (function () {
  "use strict";
  const U = window.XY.Util;

  /** 打开通用弹窗；返回 {body, close}。 */
  function open(opts) {
    close();
    const o = opts || {};
    const root = U.$("#modal-root");
    if (!root) return { body: null, close: close };
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const card = document.createElement("div");
    card.className = "modal";
    if (o.width) card.style.width = o.width;
    const head = document.createElement("div");
    head.className = "modal-head";
    const title = document.createElement("h3");
    title.textContent = o.title || "";
    head.appendChild(title);
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "modal-close";
    closeBtn.setAttribute("aria-label", "关闭");
    closeBtn.textContent = "✕";
    closeBtn.addEventListener("click", close);
    head.appendChild(closeBtn);
    const body = document.createElement("div");
    body.className = "modal-body";
    if (o.bodyHtml) body.innerHTML = o.bodyHtml;
    card.appendChild(head);
    card.appendChild(body);
    overlay.appendChild(card);
    root.appendChild(overlay);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close();
    });
    if (o.onMount) o.onMount(body, close);
    return { body: body, close: close };
  }

  /** 关闭当前弹窗（幂等）。 */
  function close() {
    const root = U.$("#modal-root");
    if (root) root.innerHTML = "";
  }

  /** 二次确认弹窗。 */
  function confirm(opts) {
    const o = opts || {};
    return open({
      title: o.title || "确认操作",
      width: "420px",
      bodyHtml: '<div class="modal-message">' + U.escapeHtml(o.message || "") + "</div>",
      onMount(body, closeFn) {
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn";
        cancel.textContent = "取消";
        cancel.addEventListener("click", closeFn);
        const ok = document.createElement("button");
        ok.type = "button";
        ok.className = "btn " + (o.danger ? "danger" : "primary");
        ok.textContent = o.confirmText || "确定";
        ok.addEventListener("click", () => {
          closeFn();
          if (o.onConfirm) o.onConfirm();
        });
        actions.appendChild(cancel);
        actions.appendChild(ok);
        body.appendChild(actions);
      },
    });
  }

  /** 表单弹窗（fields 驱动；textarea 用 type="textarea"）。 */
  function prompt(opts) {
    const o = opts || {};
    const fields = o.fields || [];
    return open({
      title: o.title || "输入",
      width: "460px",
      bodyHtml: '<form class="modal-form" id="modal-form"></form>',
      onMount(body, closeFn) {
        const form = body.querySelector("#modal-form");
        if (!form) return;
        fields.forEach((f) => {
          const label = document.createElement("label");
          label.textContent = f.label || f.key || "";
          let input;
          if (f.type === "textarea") {
            input = document.createElement("textarea");
            input.rows = f.rows || 6;
          } else {
            input = document.createElement("input");
            input.type = f.type || "text";
          }
          input.name = f.key;
          if (f.value != null) input.value = f.value;
          if (f.placeholder) input.placeholder = f.placeholder;
          if (f.required) input.required = true;
          label.appendChild(input);
          if (f.hint) {
            const hint = document.createElement("p");
            hint.className = "hint";
            hint.textContent = f.hint;
            label.appendChild(hint);
          }
          form.appendChild(label);
        });
        const actions = document.createElement("div");
        actions.className = "modal-actions";
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "btn";
        cancel.textContent = "取消";
        cancel.addEventListener("click", closeFn);
        const ok = document.createElement("button");
        ok.type = "button";
        ok.className = "btn primary";
        ok.textContent = o.confirmText || "保存";
        ok.addEventListener("click", () => {
          const values = {};
          fields.forEach((f) => {
            const el = form.elements[f.key];
            if (el) values[f.key] = el.value;
          });
          if (o.validate) {
            const err = o.validate(values);
            if (err) {
              U.toast(err, true);
              return;
            }
          }
          closeFn();
          if (o.onSave) o.onSave(values);
        });
        actions.appendChild(cancel);
        actions.appendChild(ok);
        form.appendChild(actions);
        form.addEventListener("submit", (e) => {
          e.preventDefault();
          ok.click();
        });
      },
    });
  }

  return {
    open: open,
    close: close,
    confirm: confirm,
    prompt: prompt,
  };
})();
