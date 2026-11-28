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
    if (!response.ok || data.ok === false) {
      const failure = new Error(data.message || `处理失败（${response.status}）`);
      failure.code = data.error_code || '';
      throw failure;
    }
    return data;
  }

  function sitePicker(context, field, values) {
    // 原页“转发通知网点”下拉查找：输入网点名称或编号查询，选中一项。
    const box = el('div', 'action-site');
    const input = el('input', 'action-site-input');
    input.type = 'text';
    input.placeholder = field.placeholder || '输入网点名称或编号';
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
        // 网点查询；转人员等用同一个查找框，查询种类由字段指定（lookup_kind/lookup_operation）。
        const data = await request('lookup', { ...context, kind: field.lookup_kind || 'site', operation: field.lookup_operation || 'search',
          payload: { key: input.value.trim() } });
        const sites = data.sites || [];
        note.textContent = sites.length ? '' : (field.lookup_kind ? '没有找到' : '没有找到网点');
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

  function control(field, values, context, form) {
    if (field.type === 'switch') {
      // 原页 el-switch：打开为“投诉人”，关闭为空格；旁边红字提示。
      const box = el('span', 'action-switch');
      const input = el('input');
      input.type = 'checkbox';
      input.setAttribute('aria-label', field.label);
      values[field.name] = values[field.name] ?? field.value ?? field.on;
      input.checked = values[field.name] === field.on;
      input.addEventListener('change', () => { values[field.name] = input.checked ? field.on : field.off; });
      box.append(input, el('span', '', field.on));
      if (field.note) box.append(el('span', 'action-switch-note', field.note));
      return box;
    }
    if (field.type === 'radio') {
      const box = el('span', 'action-radios');
      values[field.name] = values[field.name] ?? field.value ?? '';
      (field.options || []).forEach((option) => {
        const label = el('label', 'action-radio');
        const input = el('input');
        input.type = 'radio';
        input.name = `${field.name}-${Math.random().toString(36).slice(2)}`;
        input.value = option;
        input.checked = values[field.name] === option;
        input.addEventListener('change', () => {
          values[field.name] = option;
          // 原页选投诉人/发件人/收件人时带出姓名和电话，其他身份清空。
          if (field.autofill) {
            const fill = field.autofill.values[option] || ['', ''];
            field.autofill.targets.forEach((target, index) => {
              values[target] = fill[index] || '';
              const node = form?.querySelector(`[data-action-field="${target}"]`);
              if (node) node.value = values[target];
            });
          }
        });
        label.append(input, document.createTextNode(option));
        box.append(label);
      });
      return box;
    }
    if (field.type === 'readonly' || field.type === 'readonly_long') {
      const box = el(field.type === 'readonly_long' ? 'textarea' : 'input', 'action-readonly');
      box.readOnly = true;
      box.value = field.value || '';
      box.setAttribute('aria-label', field.label);
      if (field.lookup) {
        // 打开对话框时从原系统实时读取（如“本站待接单量”）。
        box.value = '读取中…';
        request('lookup', { ...context, kind: field.lookup.kind, operation: field.lookup.operation, payload: {} })
          .then((data) => { box.value = data.value ?? ''; })
          .catch((error) => { box.value = `读取失败：${error.message}`; });
      }
      return box;
    }
    if (field.type === 'site') return sitePicker(context, field, values);
    if (field.type === 'select') {
      const box = el('select', 'action-select');
      box.setAttribute('aria-label', field.label);
      box.dataset.actionField = field.name;
      const fill = (options) => {
        box.replaceChildren();
        [['', '请选择'], ...options.map((option) => (Array.isArray(option) ? option : [option.value ?? option, option.label ?? option]))]
          .forEach(([value, label]) => {
            const option = el('option', '', label);
            option.value = value;
            box.append(option);
          });
        box.value = values[field.name] ?? '';
        if (box.value !== (values[field.name] ?? '')) values[field.name] = '';
      };
      values[field.name] = values[field.name] ?? field.value ?? '';
      if (field.lookup) {
        // 原页下拉的数据来自原平台（如处理类型→申诉类型联动），经同一业务账号只读查询。
        const metaByValue = {};
        const load = async () => {
          const params = {};
          if (field.lookup.depends_on) {
            params[field.lookup.depends_on] = values[field.lookup.depends_on] || '';
            if (!params[field.lookup.depends_on]) { fill([]); box.disabled = true; return; }
          }
          box.disabled = true;
          try {
            const data = await request('lookup', { ...context, kind: field.lookup.kind, operation: field.lookup.operation, payload: params });
            (data.options || []).forEach((option) => { metaByValue[option.value] = option.meta || {}; });
            fill(data.options || []);
          } catch (error) {
            fill([]);
            box.title = error.message;
          }
          box.disabled = false;
          box.dispatchEvent(new Event('change'));
        };
        box.addEventListener('action:reload', load);
        box.metaFor = (value) => metaByValue[value] || {};
        window.setTimeout(load, 0);
      } else {
        fill(field.options || []);
      }
      box.addEventListener('change', () => {
        values[field.name] = box.value;
        // 依赖本字段的下拉重新读取；本字段的效果（提示文字、显示实际重量）同原页 appleTypeChangeMethod。
        form?.querySelectorAll('select').forEach((other) => {
          if (other !== box && other.dataset.dependsOn === field.name) { values[other.dataset.actionField] = ''; other.dispatchEvent(new Event('action:reload')); }
        });
        const effects = field.effects || {};
        if (effects.placeholder) {
          const target = form?.querySelector(`[data-action-field="${effects.placeholder.target}"]`);
          if (target) target.placeholder = (box.metaFor?.(box.value) || {})[effects.placeholder.meta] || '';
        }
        if (effects.show_when) {
          const row = form?.querySelector(`[data-action-row="${effects.show_when.target}"]`);
          if (row) row.hidden = box.value !== effects.show_when.equals;
        }
      });
      if (field.lookup?.depends_on) box.dataset.dependsOn = field.lookup.depends_on;
      return box;
    }
    const box = el(field.type === 'textarea' ? 'textarea' : 'input', field.type === 'textarea' ? 'action-textarea' : 'action-input');
    if (field.type === 'datetime') box.type = 'datetime-local';
    else if (field.type !== 'textarea') box.type = 'text';
    if (field.type === 'digits') box.inputMode = 'numeric';
    box.dataset.actionField = field.name;
    if (field.type === 'money') box.inputMode = 'decimal';
    if (field.max) box.maxLength = field.max;
    box.value = values[field.name] ?? field.value ?? '';
    box.setAttribute('aria-label', field.label);
    box.addEventListener('input', () => {
      // datetime-local 给 “2026-11-27T09:30”，原页格式是 “yyyy-MM-dd HH:mm”。
      values[field.name] = field.type === 'datetime' ? box.value.replace('T', ' ') : box.value;
    });
    if (field.type === 'datetime' && values[field.name]) box.value = String(values[field.name]).replace(' ', 'T');
    return box;
  }

  function missing(spec, values) {
    for (const field of spec.fields || []) {
      if (!field.required) continue;
      if (field.hidden && values[`${field.name}__shown`] === undefined) continue;
      const value = field.type === 'site' ? values.site_code : String(values[field.name] ?? field.value ?? '').trim();
      if (!value) return `${field.label}不能为空`;
      if (field.type === 'digits' && !/^\d+$/.test(value)) return `请输入正确的${field.label}`;
      if (field.max && value.length > field.max) return `${field.label}不能超过${field.max}个字`;
    }
    if (spec.attachments?.required && !(values.attachments || []).length) return '请上传附件';
    return '';
  }

  function payloadOf(spec, values) {
    const body = {};
    for (const field of spec.fields || []) {
      if (field.type === 'readonly' || field.type === 'readonly_long') continue;
      if (field.type === 'site') body[field.payload_key || 'site_code'] = values.site_code || '';
      else body[field.name] = String(values[field.name] ?? field.value ?? '');
    }
    if (spec.attachments) body.attachments = values.attachments || [];
    (spec.attachment_slots || []).forEach((slot) => { body[slot.key] = values[slot.key] || []; });
    return body;
  }

  /** Open the dialog for one spec. ``context``: platform, account_id, item. ``onDone(spec, data)`` after a verified save. */
  function open(spec, baseContext, onDone) {
    // 字段与联动特别多的原页表单有专用渲染器（投诉登记：console/static/complaint_form.js）。
    if (spec.form === 'complaint' && window.ComplaintForm) return window.ComplaintForm.open(spec, baseContext, onDone);
    if (spec.form === 'arbitration' && window.ArbitrationForm) return window.ArbitrationForm.open(spec, baseContext, onDone);
    // 合并了多个业务账号的分类（如融辉申诉登记“新增”）先选账号；后面的查询与提交都用所选账号。
    const context = { ...baseContext };
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
    if (Array.isArray(spec.account_options) && spec.account_options.length) {
      const row = el('label', 'action-row');
      const caption = el('span', 'action-label');
      caption.append(el('span', 'action-required', '*'), document.createTextNode('业务账号'));
      const picker = el('select', 'action-select');
      picker.setAttribute('aria-label', '业务账号');
      spec.account_options.forEach(([value, label]) => {
        const option = el('option', '', label);
        option.value = value;
        picker.append(option);
      });
      picker.value = context.account_id;
      if (picker.value !== context.account_id) context.account_id = picker.value;
      picker.addEventListener('change', () => {
        context.account_id = picker.value;
        form.querySelectorAll('select[data-action-field]').forEach((select) => {
          if (!select.dataset.dependsOn) select.dispatchEvent(new Event('action:reload'));
        });
      });
      row.append(caption, picker);
      form.append(row);
    }
    (spec.fields || []).forEach((field) => {
      const row = el('label', 'action-row');
      row.dataset.actionRow = field.name;
      row.hidden = Boolean(field.hidden);
      const caption = el('span', 'action-label');
      if (field.required) caption.append(el('span', 'action-required', '*'));
      caption.append(document.createTextNode(field.label));
      row.append(caption, control(field, values, context, form));
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

  /** The record's toolbar: one button per spec, in the original order.
   * 消息中心（用户 2026-11-28 要求有主次）：hideBlocked 时原页会拦下的按钮不显示（原页是点了才提示，结果相同）；
   * primary 为 `kind:operation` 时该按钮为主按钮、其余为次要按钮；labelOf 可改按钮文字。返回显示的按钮数。 */
  function renderButtons(container, actions, { onDone, hideBlocked = false, primary = '', labelOf = null } = {}) {
    container.replaceChildren();
    if (!actions || !Array.isArray(actions.specs) || !actions.specs.length) return 0;
    const context = { platform: actions.platform, account_id: actions.account_id, item: actions.item };
    const bar = el('div', 'action-bar');
    const note = el('span', 'action-bar-note');
    const specs = actions.specs.filter((spec) => !(hideBlocked && spec.blocked));
    // 主按钮排第一个，其余保持原页顺序。
    const first = specs.findIndex((spec) => `${spec.kind}:${spec.operation}` === primary);
    if (first > 0) specs.unshift(...specs.splice(first, 1));
    if (!specs.length) return 0;
    specs.forEach((spec) => {
      const key = `${spec.kind}:${spec.operation}`;
      const tone = !primary ? '' : key === primary ? ' is-primary' : ' is-quiet';
      const button = el('button', `action-button${tone}`, labelOf ? labelOf(spec) : spec.label);
      button.type = 'button';
      button.dataset.operation = spec.operation;
      let requestId = uuid();
      button.addEventListener('click', async () => {
        if (spec.blocked) { note.textContent = spec.blocked; return; }
        note.textContent = '';
        if (!spec.form && !(spec.fields || []).length && !spec.attachments && !(spec.attachment_slots || []).length) {
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
    return specs.length;
  }

  window.OriginalPageActions = { request, renderButtons, open, sitePicker };
})();
