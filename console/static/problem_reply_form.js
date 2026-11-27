/* Problem reply form as the original pages show it (2026-11-27). Rendered only from the
 * ``reply_form`` the Agent attaches to each problem (shared/problem_reply_rules.py), so the
 * customer-service page and the home message center share one form. Every value is checked
 * again by the Agent before it writes; its message is shown as is. Text only via textContent. */
(() => {
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const DRIVER_FIELDS = [
    ['name', '司机姓名', true], ['phone', '手机号', true], ['idCard', '身份证号（选填）', false],
    ['plate', '车牌号', true], ['count', '件数', true]
  ];

  function freshDraft(form) {
    return {
      reply_text: '', prob_status: form?.default_status || '', review_entry_status: '', pick_type: '',
      pickup_day: '', pickup_hour: '', return_opinion: '', drivers: [{ name: '', phone: '', idCard: '', plate: '', count: '' }]
    };
  }

  function radios(name, label, options, value, onPick) {
    const box = el('fieldset', 'reply-form-choice');
    box.append(el('legend', '', label));
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

  function select(label, options, value, onPick, placeholder) {
    const wrap = el('label', 'reply-form-field');
    wrap.append(el('span', '', label));
    const node = el('select');
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
    wrap.append(node);
    return wrap;
  }

  function driversBlock(form, draft, rerender) {
    const box = el('div', 'reply-form-drivers');
    draft.drivers.forEach((driver, index) => {
      const row = el('div', 'reply-form-driver');
      row.append(el('span', 'reply-form-driver-no', `司机${index + 1}`));
      DRIVER_FIELDS.forEach(([key, label, required]) => {
        const input = el('input');
        input.value = driver[key] || '';
        input.placeholder = label;
        input.setAttribute('aria-label', `司机${index + 1}${label}`);
        if (key === 'count') input.inputMode = 'numeric';
        if (key === 'phone') input.inputMode = 'tel';
        input.required = required;
        input.addEventListener('input', () => { driver[key] = input.value; });
        row.append(input);
      });
      if (draft.drivers.length > 1) {
        const remove = el('button', 'reply-form-link', '删除');
        remove.type = 'button';
        remove.addEventListener('click', () => { draft.drivers.splice(index, 1); rerender(); });
        row.append(remove);
      }
      box.append(row);
    });
    if (draft.drivers.length < (form.max_drivers || 5)) {
      const add = el('button', 'reply-form-link', '＋ 添加司机');
      add.type = 'button';
      add.addEventListener('click', () => {
        draft.drivers.push({ name: '', phone: '', idCard: '', plate: '', count: '' });
        rerender();
      });
      box.append(add);
    }
    if (typeof form.piece_number === 'number') box.append(el('p', 'reply-form-note', `开单件数 ${form.piece_number}，各司机件数合计不能超过它。`));
    return box;
  }

  /** Render the form into ``container``; ``draft`` keeps the values across re-renders.
   *  ``textAttr`` names an extra attribute for the reply textarea (the customer-service page's AI hint fills it). */
  function render(container, form, draft, { textAttr = '' } = {}) {
    container.replaceChildren();
    if (!form) {
      container.append(el('p', 'reply-form-note', '这条问题件没有可用的回复表单，请刷新后重试。'));
      return false;
    }
    if (form.blocked) {
      container.append(el('p', 'reply-form-note is-blocked', form.blocked));
      return false;
    }
    const rerender = () => render(container, form, draft, { textAttr });
    if (form.platform === 'yunda') {
      container.append(select('处理状态', (form.statuses || []).map((item) => [item.value, item.label]),
        draft.prob_status || form.default_status, (value) => { draft.prob_status = value; }));
      if (form.review_entry) {
        container.append(radios(`review-${Math.random().toString(36).slice(2)}`, '预约进仓（原系统有审核权限时必选）',
          [['1', '同意'], ['2', '不同意']], draft.review_entry_status, (value) => { draft.review_entry_status = value; }));
      }
    } else {
      if ((form.return_opinions || []).length) {
        container.append(radios(`return-${Math.random().toString(36).slice(2)}`, '退件处理意见',
          form.return_opinions.map((value) => [value, value]), draft.return_opinion, (value) => { draft.return_opinion = value; }));
      }
      if (form.self_pick) {
        container.append(radios(`pick-${Math.random().toString(36).slice(2)}`, '提货方式',
          form.pick_types.map((value) => [value, value]), draft.pick_type, (value) => { draft.pick_type = value; rerender(); }));
        if (form.driver_pick_types.includes(draft.pick_type)) {
          container.append(driversBlock(form, draft, rerender));
          const when = el('div', 'reply-form-row');
          when.append(
            select('提货日期', form.pickup_days.map((day) => [day, day]), draft.pickup_day, (value) => { draft.pickup_day = value; }, '请选择日期'),
            select('提货时间段', form.pickup_hours.map((hour) => [hour, hour]), draft.pickup_hour, (value) => { draft.pickup_hour = value; }, '请选择时间段'));
          container.append(when);
        }
      }
    }
    const textWrap = el('label', 'reply-form-field reply-form-text');
    textWrap.append(el('span', '', form.platform === 'yunda' ? '回复正文' : '回复'));
    const text = el('textarea');
    text.rows = 3;
    text.maxLength = form.reply_max || 500;
    text.placeholder = `最多 ${text.maxLength} 字，提交后写入原平台并核对`;
    text.value = draft.reply_text;
    text.dataset.replyFormText = '';
    if (textAttr) text.setAttribute(textAttr, '');
    text.addEventListener('input', () => { draft.reply_text = text.value; });
    textWrap.append(text);
    container.append(textWrap);
    return true;
  }

  /** The ``payload`` for /customer-service/problems/reply, read from the rendered form. */
  function payload(container, form, draft) {
    const text = container.querySelector('[data-reply-form-text]');
    if (text) draft.reply_text = text.value;  // the AI hint may have filled it without an input event
    const body = { reply_text: draft.reply_text.trim() };
    if (form?.platform === 'yunda') {
      body.prob_status = draft.prob_status || form.default_status;
      if (draft.review_entry_status) body.review_entry_status = draft.review_entry_status;
      return body;
    }
    if (draft.return_opinion) body.return_opinion = draft.return_opinion;
    if (form?.self_pick && draft.pick_type) {
      body.pick_type = draft.pick_type;
      if (form.driver_pick_types.includes(draft.pick_type)) {
        body.drivers = draft.drivers.filter((driver) => String(driver.name || '').trim())
          .map((driver) => Object.fromEntries(DRIVER_FIELDS.map(([key]) => [key, String(driver[key] || '').trim()])));
        body.pickup_day = draft.pickup_day;
        body.pickup_hour = draft.pickup_hour;
      }
    }
    return body;
  }

  window.ProblemReplyForm = { render, payload, freshDraft };
})();
