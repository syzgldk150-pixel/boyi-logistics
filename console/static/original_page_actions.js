/* Processing actions laid out like the original pages (2026-11-27): the toolbar buttons of a record
 * (融辉工单 响应/完结/催工单/申诉 …) and the dialog each opens, rendered only from the specs the
 * Agent attaches (shared/original_page_actions.py). A button whose original check fails shows the
 * page's own message instead of opening the dialog (the original alerts it on click). Saving posts
 * to /customer-service/process with the record's business account; the Agent re-reads the record,
 * checks again, saves and reads back, and its message is shown as is. The same request UUID is
 * reused only after a network failure (outcome unknown), a new one after a refused save.
 * Text only via textContent. */
(() => {
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const uuid = () => (window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
  const fetcher = () => (window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window));

  /** POST /customer-service/{process|lookup}; resolves to the JSON body, throws Error(message) with
   *  ``error.network`` true when the outcome is unknown. */
  async function request(action, body, requestId) {
    let response;
    try {
      response = await fetcher()(`/customer-service/${action}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Browser-Request-UUID': requestId || uuid() },
        body: JSON.stringify(body)
      });
    } catch (error) {
      const failure = new Error('网络中断，结果未知，请稍后用同一操作重试或到原系统核对。');
      failure.network = true;
      throw failure;
    }
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false) throw new Error(data.message || `处理失败（${response.status}）`);
    return data;
  }

  function sitePicker(context, field, values) {
    // 原页“转发通知网点”下拉查找：输入网点名称或编号查询，选中一项。
    const box = el('div', 'action-site');
    const input = el('input', 'action-site-input');
    input.type = 'text';
    input.placeholder = '输入网点名称或编号';
    input.value = values[`${field.name}_text`] || field.value || '';
    input.setAttribute('aria-label', field.label);
    const search = el('button', 'action-site-search', '查询');
    search.type = 'button';
    const list = el('ul', 'action-site-list');
    const note = el('span', 'action-site-note');
    search.addEventListener('click', async () => {
      note.textContent = '查询中…';
      list.replaceChildren();
      try {
        const data = await request('lookup', { ...context, kind: 'site', operation: 'search', payload: { key: input.value.trim() } });
        const sites = data.sites || [];
        note.textContent = sites.length ? '' : '没有找到网点';
        sites.forEach((site) => {
          const option = el('li');
          const pick = el('button', 'action-site-option', `${site.SITE_CODE} ${site.SITE_NAME}`);
          pick.type = 'button';
          pick.addEventListener('click', () => {
            values.site_code = site.SITE_CODE;
            values[`${field.name}_text`] = site.SITE_NAME;
            input.value = site.SITE_NAME;
            list.replaceChildren();
            note.textContent = `已选：${site.SITE_CODE}`;
          });
          option.append(pick);
          list.append(option);
        });
      } catch (error) {
        note.textContent = error.message;
      }
    });
    input.addEventListener('input', () => { delete values.site_code; note.textContent = ''; });
    input.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); search.click(); } });
    box.append(input, search, note, list);
    return box;
  }

  function control(field, values, context) {
    if (field.type === 'readonly' || field.type === 'readonly_long') {
      const box = el(field.type === 'readonly_long' ? 'textarea' : 'input', 'action-readonly');
      box.readOnly = true;
      box.value = field.value || '';
      box.setAttribute('aria-label', field.label);
      return box;
    }
    if (field.type === 'site') return sitePicker(context, field, values);
    if (field.type === 'select') {
      const box = el('select', 'action-select');
      box.setAttribute('aria-label', field.label);
      [['', '请选择'], ...(field.options || []).map((option) => (Array.isArray(option) ? option : [option, option]))].forEach(([value, label]) => {
        const option = el('option', '', label);
        option.value = value;
        box.append(option);
      });
      box.value = values[field.name] ?? field.value ?? '';
      box.addEventListener('change', () => { values[field.name] = box.value; });
      return box;
    }
    const box = el(field.type === 'textarea' ? 'textarea' : 'input', field.type === 'textarea' ? 'action-textarea' : 'action-input');
    if (field.type !== 'textarea') box.type = 'text';
    if (field.type === 'money') box.inputMode = 'decimal';
    if (field.max) box.maxLength = field.max;
    box.value = values[field.name] ?? field.value ?? '';
    box.setAttribute('aria-label', field.label);
    box.addEventListener('input', () => { values[field.name] = box.value; });
    return box;
  }

  function missing(spec, values) {
    for (const field of spec.fields || []) {
      if (!field.required) continue;
      const value = field.type === 'site' ? values.site_code : String(values[field.name] ?? field.value ?? '').trim();
      if (!value) return `${field.label}不能为空`;
    }
    if (spec.attachments?.required && !(values.attachments || []).length) return '请上传附件';
    return '';
  }

  function payloadOf(spec, values) {
    const body = {};
    for (const field of spec.fields || []) {
      if (field.type === 'readonly' || field.type === 'readonly_long') continue;
      if (field.type === 'site') body.site_code = values.site_code || '';
      else body[field.name] = String(values[field.name] ?? field.value ?? '');
    }
    if (spec.attachments) body.attachments = values.attachments || [];
    (spec.attachment_slots || []).forEach((slot) => { body[slot.key] = values[slot.key] || []; });
    return body;
  }

  /** Open the dialog for one spec. ``context``: platform, account_id, item. ``onDone(spec, data)`` after a verified save. */
  function open(spec, context, onDone) {
    const dialog = el('dialog', 'action-dialog');
    dialog.setAttribute('aria-label', spec.title || spec.label);
    const values = {};
    let requestId = uuid();
    const head = el('div', 'action-dialog-head');
    head.append(el('h3', '', spec.title || spec.label));
    const close = el('button', 'action-dialog-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭');
    head.append(close);
    const form = el('div', 'action-dialog-body');
    (spec.fields || []).forEach((field) => {
      const row = el('label', 'action-row');
      const caption = el('span', 'action-label');
      if (field.required) caption.append(el('span', 'action-required', '*'));
      caption.append(document.createTextNode(field.label));
      row.append(caption, control(field, values, context));
      form.append(row);
    });
    if (spec.attachments && window.OriginalPageAttachments) {
      const files = el('div', 'action-files');
      values.attachments = [];
      window.OriginalPageAttachments.render(files, { platform: context.platform, accountId: context.account_id, purpose: spec.attachments.purpose }, values.attachments);
      form.append(files);
    }
    // 原页固定的图片格（如“面单附件图片”“货物附件图片”“价值正面图片”），每格一张。
    (spec.attachment_slots || []).forEach((slot) => {
      if (!window.OriginalPageAttachments) return;
      const row = el('div', 'action-row');
      row.append(el('span', 'action-label', slot.label));
      const files = el('div', 'action-files');
      values[slot.key] = [];
      window.OriginalPageAttachments.render(files, { platform: context.platform, accountId: context.account_id, purpose: slot.purpose }, values[slot.key]);
      row.append(files);
      form.append(row);
    });
    const foot = el('div', 'action-dialog-foot');
    const message = el('span', 'action-message');
    const save = el('button', 'action-save', '保存');
    save.type = 'button';
    const cancel = el('button', 'action-cancel', '取消');
    cancel.type = 'button';
    foot.append(message, save, cancel);
    dialog.append(head, form, foot);
    const shut = () => { dialog.close(); dialog.remove(); };
    close.addEventListener('click', shut);
    cancel.addEventListener('click', shut);
    save.addEventListener('click', async () => {
      const problem = missing(spec, values);
      if (problem) { message.textContent = problem; message.dataset.tone = 'error'; return; }
      if (spec.confirm && !window.confirm(spec.confirm)) return;
      save.disabled = true;
      cancel.disabled = true;
      message.dataset.tone = '';
      message.textContent = '保存中…';
      try {
        const data = await request('process', { ...context, kind: spec.kind, operation: spec.operation, payload: payloadOf(spec, values) }, requestId);
        message.textContent = '数据保存成功';
        onDone?.(spec, data);
        window.setTimeout(shut, 600);
      } catch (error) {
        if (!error.network) requestId = uuid();
        message.textContent = error.message;
        message.dataset.tone = 'error';
        save.disabled = false;
        cancel.disabled = false;
      }
    });
    document.body.append(dialog);
    dialog.showModal();
    return dialog;
  }

  /** The record's toolbar: one button per spec, in the original order. */
  function renderButtons(container, actions, { onDone } = {}) {
    container.replaceChildren();
    if (!actions || !Array.isArray(actions.specs) || !actions.specs.length) return false;
    const context = { platform: actions.platform, account_id: actions.account_id, item: actions.item };
    const bar = el('div', 'action-bar');
    const note = el('span', 'action-bar-note');
    actions.specs.forEach((spec) => {
      const button = el('button', 'action-button', spec.label);
      button.type = 'button';
      button.dataset.operation = spec.operation;
      let requestId = uuid();
      button.addEventListener('click', async () => {
        if (spec.blocked) { note.textContent = spec.blocked; return; }
        note.textContent = '';
        if (!(spec.fields || []).length && !spec.attachments && !(spec.attachment_slots || []).length) {
          // 原页直接确认的操作（取消投诉、删除、撤销）：确认后提交，结果写在按钮旁。
          if (spec.confirm && !window.confirm(spec.confirm)) return;
          button.disabled = true;
          note.textContent = `${spec.label}中…`;
          try {
            const data = await request('process', { ...context, kind: spec.kind, operation: spec.operation, payload: {} }, requestId);
            note.textContent = `已${spec.label}`;
            onDone?.(spec, data);
          } catch (error) {
            if (!error.network) requestId = uuid();
            note.textContent = error.message;
            button.disabled = false;
          }
          return;
        }
        open(spec, context, (done, data) => {
          note.textContent = `${done.label}已保存`;
          onDone?.(done, data);
        });
      });
      bar.append(button);
    });
    bar.append(note);
    container.append(bar);
    return true;
  }

  window.OriginalPageActions = { request, renderButtons, open, sitePicker };
})();
