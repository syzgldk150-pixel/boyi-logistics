/* Problem reply form laid out and behaving like the original pages (2026-11-27; read-only capture,
 * see docs/original_page_processing_inventory.md):
 * - 融辉 “收到问题件查询” reply area: 问题原因 (read only) on the left; on the right the hint
 *   “在回复框内，按CTRL+ENTER保存”, 提货方式 (self-pickup problems), the driver box (hint, counter
 *   “当前已添加 N 名司机（最多5名）”, first row with 件数 prefilled from 开单件数 and 添加司机 at its
 *   end, further rows with 删除司机, checks on leaving each box with the message under it),
 *   提货时间 (day + hour), 退件处理意见 (退件 problems), 回复, then 保存 / 取消.
 * - 韵达 query/reply.html: 问题状态, “* 审核预约进仓” 同意/不同意 (type 572), 回复正文 with “0/600”,
 *   then 保存 / 取消.
 * Rendered only from the problem's ``reply_form`` (shared/problem_reply_rules.py). The Agent checks
 * every value again before writing and its message is shown as is. Text only via textContent. */
(() => {
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const uid = () => Math.random().toString(36).slice(2);
  // Same checks and messages as the original page's validateInput (shown when leaving a box).
  const DRIVER_FIELDS = [
    { key: 'name', label: '司机姓名', required: true },
    { key: 'phone', label: '手机号', required: true, type: 'tel', test: /^1[3-9]\d{9}$/, message: '手机号格式不正确' },
    { key: 'idCard', label: '身份证号', required: false, test: /(^\d{15}$)|(^\d{18}$)|(^\d{17}(\d|X|x)$)/, message: '身份证号格式不正确' },
    { key: 'plate', label: '车牌号', required: true, test: /^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}$/, message: '车牌号格式不正确' },
    { key: 'count', label: '件数', required: true, type: 'number', check: (value) => Number.parseInt(value, 10) > 0, message: '件数必须是大于0的数字' }
  ];

  function driverRow(form) {
    return { name: '', phone: '', idCard: '', plate: '', count: typeof form?.piece_number === 'number' ? String(form.piece_number) : '' };
  }

  function freshDraft(form) {
    return {
      reply_text: '', prob_status: form?.default_status || '', review_entry_status: '', pick_type: '',
      pickup_day: '', pickup_hour: '', return_opinion: '', drivers: [], attachments: []
    };
  }

  function radioList(name, options, value, onPick) {
    const box = el('div', 'reply-form-radios');
    options.forEach(([optionValue, optionLabel]) => {
      const item = el('label', 'reply-form-radio');
      const input = el('input');
      input.type = 'radio';
      input.name = name;
      input.value = optionValue;
      input.checked = optionValue === value;
      input.addEventListener('change', () => onPick(optionValue));
      item.append(input, el('span', '', optionLabel));
      box.append(item);
    });
    return box;
  }

  function selectBox(options, value, onPick, placeholder, label) {
    const node = el('select');
    node.setAttribute('aria-label', label);
    if (placeholder) {
      const empty = el('option', '', placeholder);
      empty.value = '';
      node.append(empty);
    }
    options.forEach(([optionValue, optionLabel]) => {
      const option = el('option', '', optionLabel);
      option.value = optionValue;
      option.selected = optionValue === value;
      node.append(option);
    });
    node.addEventListener('change', () => onPick(node.value));
    return node;
  }

  function checkDriverInput(field, value) {
    if (field.required && !value) return '此项为必填项';
    if (value && field.test && !field.test.test(value)) return field.message;
    if (value && field.check && !field.check(value)) return field.message;
    return '';
  }

  function driverBox(form, draft, rerender) {
    const box = el('div', 'reply-form-driver-box');
    box.append(el('p', 'reply-form-hint', '提货人超过五个时，可将提货人信息写在回复框内'));
    box.append(el('div', 'reply-form-counter', `当前已添加 ${draft.drivers.length} 名司机（最多${form.max_drivers || 5}名）`));
    draft.drivers.forEach((driver, index) => {
      const row = el('div', 'reply-form-driver');
      DRIVER_FIELDS.forEach((field) => {
        const wrap = el('div', 'reply-form-driver-input');
        const input = el('input');
        input.type = field.type || 'text';
        input.placeholder = field.label;
        input.value = driver[field.key] || '';
        input.required = field.required;
        input.setAttribute('aria-label', `司机${index + 1}${field.label}`);
        const error = el('div', 'reply-form-error');
        input.addEventListener('input', () => { driver[field.key] = input.value; });
        input.addEventListener('blur', () => {
          const message = checkDriverInput(field, input.value.trim());
          error.textContent = message;
          input.classList.toggle('is-error', Boolean(message));
        });
        wrap.append(input, error);
        row.append(wrap);
      });
      if (index === 0) {
        const full = draft.drivers.length >= (form.max_drivers || 5);
        const add = el('button', 'reply-form-add', full ? `已达上限(${form.max_drivers || 5}名)` : '添加司机');
        add.type = 'button';
        add.disabled = full;
        add.addEventListener('click', () => { draft.drivers.push(driverRow(null)); rerender(); });
        row.append(add);
      } else {
        const remove = el('button', 'reply-form-remove', '删除司机');
        remove.type = 'button';
        remove.addEventListener('click', () => { draft.drivers.splice(index, 1); rerender(); });
        row.append(remove);
      }
      box.append(row);
    });
    return box;
  }

  function line(label, control, { required = false } = {}) {
    const row = el('div', 'reply-form-line');
    const caption = el('span', 'reply-form-label');
    if (required) caption.append(el('span', 'reply-form-required', '*'));
    caption.append(document.createTextNode(label));
    row.append(caption, control);
    return row;
  }

  function actions(options, draft) {
    const bar = el('div', 'reply-form-actions');
    const note = el('span', 'reply-form-message', draft.error || '');
    if (draft.error) note.dataset.tone = 'error';
    const save = el('button', 'reply-form-save', draft.sending ? '保存中…' : '保存');
    save.type = 'button';
    save.disabled = Boolean(draft.sending);
    save.addEventListener('click', () => options.onSave?.());
    const cancel = el('button', 'reply-form-cancel', '取消');
    cancel.type = 'button';
    cancel.disabled = Boolean(draft.sending);
    cancel.addEventListener('click', () => options.onCancel?.());
    bar.append(note, save, cancel);
    return bar;
  }

  function replyText(form, draft, options) {
    const text = el('textarea', 'reply-form-text');
    text.rows = form.platform === 'yunda' ? 5 : 4;
    text.maxLength = form.reply_max || 500;
    text.value = draft.reply_text;
    text.dataset.replyFormText = '';
    text.setAttribute('aria-label', form.platform === 'yunda' ? '回复正文' : '回复');
    if (options.textAttr) text.setAttribute(options.textAttr, '');
    return text;
  }

  function renderRonghui(container, form, draft, options, rerender) {
    const layout = el('div', 'reply-form-ronghui');
    const cause = el('div', 'reply-form-cause');
    const causeText = el('textarea');
    causeText.readOnly = true;
    causeText.value = form.problem_cause || '';
    causeText.setAttribute('aria-label', '问题原因');
    cause.append(el('span', 'reply-form-label', '问题原因'), causeText);
    const side = el('div', 'reply-form-side');
    side.append(el('p', 'reply-form-hint', '在回复框内，按CTRL+ENTER保存'));
    if (form.self_pick) {
      side.append(radioList(`pick-${uid()}`, form.pick_types.map((value) => [value, value]), draft.pick_type, (value) => {
        draft.pick_type = value;
        // 原页切换提货方式时清空司机与提货时间，并重新生成第一行。
        draft.drivers = form.driver_pick_types.includes(value) ? [driverRow(form)] : [];
        draft.pickup_day = '';
        draft.pickup_hour = '';
        rerender();
      }));
      if (form.driver_pick_types.includes(draft.pick_type)) {
        side.append(driverBox(form, draft, rerender));
        const when = el('div', 'reply-form-inline');
        when.append(
          selectBox(form.pickup_days.map((day) => [day, day]), draft.pickup_day, (value) => { draft.pickup_day = value; }, '请选择日期', '提货日期'),
          selectBox(form.pickup_hours.map((hour) => [hour, hour]), draft.pickup_hour, (value) => { draft.pickup_hour = value; }, '请选择时间段', '提货时间段'));
        side.append(line('提货时间', when));
      }
    }
    if ((form.return_opinions || []).length) {
      side.append(line('退件处理意见', radioList(`return-${uid()}`, form.return_opinions.map((value) => [value, value]),
        draft.return_opinion, (value) => { draft.return_opinion = value; })));
    }
    const text = replyText(form, draft, options);
    text.addEventListener('input', () => { draft.reply_text = text.value; });
    text.addEventListener('keydown', (event) => {
      if (event.ctrlKey && event.key === 'Enter') { event.preventDefault(); options.onSave?.(); }
    });
    side.append(line('回复', text), actions(options, draft));
    layout.append(cause, side);
    container.append(layout);
  }

  function renderYunda(container, form, draft, options) {
    const box = el('div', 'reply-form-yunda');
    box.append(line('问题状态', selectBox((form.statuses || []).map((item) => [item.value, item.label]),
      draft.prob_status || form.default_status, (value) => { draft.prob_status = value; }, '', '问题状态')));
    if (form.review_entry) {
      box.append(line('审核预约进仓:', radioList(`review-${uid()}`, [['1', '同意'], ['2', '不同意']],
        draft.review_entry_status, (value) => { draft.review_entry_status = value; }), { required: true }));
    }
    const text = replyText(form, draft, options);
    const counter = el('div', 'reply-form-count', `${draft.reply_text.length}/${text.maxLength}`);
    text.addEventListener('input', () => { draft.reply_text = text.value; counter.textContent = `${text.value.length}/${text.maxLength}`; });
    const wrap = el('div', 'reply-form-text-wrap');
    wrap.append(text, counter);
    box.append(line('回复正文', wrap, { required: true }));
    // 原页回复正文下方的附件选择（最多3个，最大5M，支持拖拽），console/static/original_page_attachments.js。
    if (window.OriginalPageAttachments && options.accountId) {
      const files = el('div', 'reply-form-files');
      window.OriginalPageAttachments.render(files, { platform: 'yunda', accountId: options.accountId, purpose: 'problem_reply' },
        draft.attachments || (draft.attachments = []));
      box.append(files);
    }
    box.append(actions(options, draft));
    container.append(box);
  }

  /** Render the form into ``container``; ``draft`` keeps values, ``draft.sending``/``draft.error`` the save state.
   *  ``options``: ``onSave``, ``onCancel``, ``textAttr`` (extra attribute on the reply box), ``accountId``
   *  (the problem's business account, for attachment uploads). Returns false when the
   *  problem cannot be replied to (the reason is shown instead). */
  function render(container, form, draft, options = {}) {
    container.replaceChildren();
    if (!form) {
      container.append(el('p', 'reply-form-note', '这条问题件没有可用的回复表单，请刷新后重试。'));
      return false;
    }
    if (form.blocked) {
      container.append(el('p', 'reply-form-note is-blocked', form.blocked));
      return false;
    }
    const rerender = () => {
      const text = container.querySelector('[data-reply-form-text]');
      if (text) draft.reply_text = text.value;
      render(container, form, draft, options);
    };
    if (form.platform === 'yunda') renderYunda(container, form, draft, options);
    else renderRonghui(container, form, draft, options, rerender);
    return true;
  }

  /** What the original page checks in the browser before saving (form required fields, then every
   *  driver box like validateAllDrivers); ``""`` when it may be sent. The Agent checks everything again. */
  function validate(container, form, draft) {
    if (!form || form.platform === 'yunda') return '';
    if (form.self_pick && !draft.pick_type) return '请选择提货方式';
    if ((form.return_opinions || []).length && !draft.return_opinion) return '请选择退件处理意见';
    if (form.self_pick && form.driver_pick_types.includes(draft.pick_type)) {
      if (!draft.pickup_day || !draft.pickup_hour) return '请选择提货时间';
      let complete = true;
      container.querySelectorAll('.reply-form-driver input').forEach((input) => {
        input.dispatchEvent(new Event('blur'));
        if (input.classList.contains('is-error')) complete = false;
      });
      if (!complete) return '司机信息填完整!';
    }
    return '';
  }

  /** The ``payload`` for /customer-service/problems/reply, read from the rendered form. */
  function payload(container, form, draft) {
    const text = container.querySelector('[data-reply-form-text]');
    if (text) draft.reply_text = text.value;  // the AI hint may have filled it without an input event
    const body = { reply_text: draft.reply_text.trim() };
    if (form?.platform === 'yunda') {
      body.prob_status = draft.prob_status || form.default_status;
      if (draft.review_entry_status) body.review_entry_status = draft.review_entry_status;
      if ((draft.attachments || []).length) body.attachments = draft.attachments;
      return body;
    }
    if (draft.return_opinion) body.return_opinion = draft.return_opinion;
    if (form?.self_pick && draft.pick_type) {
      body.pick_type = draft.pick_type;
      if (form.driver_pick_types.includes(draft.pick_type)) {
        body.drivers = draft.drivers.filter((driver) => String(driver.name || '').trim())
          .map((driver) => Object.fromEntries(DRIVER_FIELDS.map(({ key }) => [key, String(driver[key] || '').trim()])));
        body.pickup_day = draft.pickup_day;
        body.pickup_hour = draft.pickup_hour;
      }
    }
    return body;
  }

  window.ProblemReplyForm = { render, payload, validate, freshDraft };
})();
