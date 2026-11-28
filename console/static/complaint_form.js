/* 融辉投诉方登记“新增”“修改”的对话框，按原页子页“投诉登记-异形件”的字段顺序与联动（2026-11-28）：
 * 运单编号带出寄件/创建/签收日期与产品类型；处理类型 → 投诉类别 → 投诉类型三级联动，投诉类型决定被投诉网点的
 * 可选范围、附件科目（逐科目上传，必填科目须有附件）、处理部门、类型描述与哪些项必填；对象（投诉中心/网点、部门、
 * 投诉人）切换被投诉方；投诉代登记时可选投诉网点；被投诉网点是不参与扣款的操作场时按原页询问是否改绑网点；
 * 围板箱录单不规范时选围板箱编号；破损类要填是否修复与未修复原因。字段与选项都来自 spec 和原平台只读查询
 * （/customer-service/lookup），隐藏字段、全部检查、写入与回读在 Agent（shared/original_page_actions.py）。
 * Text only via textContent. */
(() => {
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const uuid = () => (window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

  function open(spec, baseContext, onDone) {
    const api = window.OriginalPageActions;
    const context = { ...baseContext };
    const editing = spec.operation === 'update';
    const values = { ...(spec.values || {}) };
    const state = { meta: {}, subjects: [], files: {}, existing: [], removed: new Set(), activeSubject: '' };
    let requestId = uuid();
    const lookup = (operation, payload) => api.request('lookup', { ...context, kind: 'complaint_form', operation, payload });

    const dialog = el('dialog', 'action-dialog complaint-dialog');
    dialog.setAttribute('aria-label', spec.title);
    const head = el('div', 'action-dialog-head');
    head.append(el('h3', '', spec.title));
    const close = el('button', 'action-dialog-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭');
    head.append(close);
    const body = el('div', 'action-dialog-body complaint-body');
    const rows = {};
    const row = (name, label, control, { required = false } = {}) => {
      const line = el('label', 'action-row');
      line.dataset.actionRow = name;
      const caption = el('span', 'action-label');
      const star = el('span', 'action-required', '*');
      star.hidden = !required;
      caption.append(star, document.createTextNode(label));
      line.append(caption, control);
      body.append(line);
      rows[name] = { line, star };
      return line;
    };
    const setRequired = (name, required) => { if (rows[name]) rows[name].star.hidden = !required; };
    const show = (name, visible) => { if (rows[name]) rows[name].line.hidden = !visible; };
    const text = (name, { readonly = false, multiline = false, placeholder = '' } = {}) => {
      const box = el(multiline ? 'textarea' : 'input', readonly ? 'action-readonly' : (multiline ? 'action-textarea' : 'action-input'));
      if (!multiline) box.type = 'text';
      box.readOnly = readonly;
      box.value = values[name] || '';
      if (placeholder) box.placeholder = placeholder;
      box.dataset.actionField = name;
      box.addEventListener('input', () => { values[name] = box.value; });
      return box;
    };
    const select = (name, options = []) => {
      const box = el('select', 'action-select');
      box.dataset.actionField = name;
      const fill = (list) => {
        box.replaceChildren();
        [{ value: '', label: '请选择' }, ...list].forEach((item) => {
          const option = el('option', '', item.label);
          option.value = item.value;
          box.append(option);
        });
        box.value = values[name] || '';
        if (box.value !== (values[name] || '')) values[name] = '';
      };
      fill(options);
      box.fill = fill;
      box.addEventListener('change', () => { values[name] = box.value; });
      return box;
    };
    const radios = (name, options, onPick) => {
      const box = el('span', 'action-radios');
      const group = `${name}-${uuid()}`;
      options.forEach(([value, label]) => {
        const item = el('label', 'action-radio');
        const input = el('input');
        input.type = 'radio';
        input.name = group;
        input.value = value;
        input.checked = (values[name] || '') === value;
        input.addEventListener('change', () => { values[name] = value; onPick?.(value); });
        item.append(input, document.createTextNode(label));
        box.append(item);
      });
      box.setValue = (value) => { box.querySelectorAll('input').forEach((input) => { input.checked = input.value === value; }); };
      return box;
    };
    const picker = (name, nameKey, operation, extra = () => ({}), onPick = null) => {
      // 原页 uc-lookup-combobox：输入名称或编号查询，选中一项。
      const box = el('div', 'action-site');
      const input = el('input', 'action-site-input');
      input.type = 'text';
      input.placeholder = '输入名称或编号';
      input.value = values[nameKey] || values[name] || '';
      input.setAttribute('aria-label', name);
      const search = el('button', 'action-site-search', '查询');
      search.type = 'button';
      const note = el('span', 'action-site-note');
      const list = el('ul', 'action-site-list');
      search.addEventListener('click', async () => {
        note.textContent = '查询中…';
        list.replaceChildren();
        try {
          const data = await lookup(operation, { key: input.value.trim(), ...extra() });
          note.textContent = (data.sites || []).length ? '' : '没有找到';
          (data.sites || []).forEach((site) => {
            const item = el('li');
            const pick = el('button', 'action-site-option', `${site.SITE_CODE} ${site.SITE_NAME}`);
            pick.type = 'button';
            pick.addEventListener('click', () => {
              values[name] = site.SITE_CODE;
              values[nameKey] = site.SITE_NAME;
              input.value = site.SITE_NAME;
              list.replaceChildren();
              note.textContent = `已选：${site.SITE_CODE}`;
              onPick?.(site, note, input);
            });
            item.append(pick);
            list.append(item);
          });
        } catch (error) {
          note.textContent = error.message;
        }
      });
      input.addEventListener('input', () => { values[name] = ''; note.textContent = ''; });
      box.append(input, search, note, list);
      box.setDisabled = (disabled) => { input.disabled = disabled; search.disabled = disabled; if (disabled) { values[name] = ''; input.value = ''; note.textContent = ''; } };
      return box;
    };

    // ---- 字段（原页顺序） ----
    const sendDate = text('SEND_DATE', { readonly: true });
    row('SEND_DATE', '寄件日期', sendDate);
    const registerDate = text('REGISTER_DATE', { readonly: true });
    row('REGISTER_DATE', '运单创建日期', registerDate);
    const signDate = text('SIGN_DATE', { readonly: true });
    row('SIGN_DATE', '运单签收日期', signDate);
    row('EXCEPTION_MAN_TYPE', '投诉人类型', select('EXCEPTION_MAN_TYPE', [['中心/网点', '中心/网点'], ['寄件人', '寄件人'], ['收件人', '收件人']].map(([value, label]) => ({ value, label }))));
    const billCode = text('BILL_CODE');
    billCode.setAttribute('aria-label', '运单编号');
    billCode.disabled = editing;
    const billNote = el('span', 'action-site-note');
    const billBox = el('div', 'action-site');
    billBox.append(billCode, billNote);
    row('BILL_CODE', '运单编号', billBox, { required: true });
    const productType = text('PRODUCT_TYPE', { readonly: true });
    const loadBill = async () => {
      if (!values.BILL_CODE) { billNote.textContent = '请输入运单号或者转单号信息！'; return; }
      billNote.textContent = '查询中…';
      try {
        const data = await lookup('bill', { BILL_CODE: values.BILL_CODE });
        ['SEND_DATE', 'REGISTER_DATE', 'SIGN_DATE', 'PRODUCT_TYPE'].forEach((key) => { values[key] = data.bill?.[key] || ''; });
        sendDate.value = values.SEND_DATE; registerDate.value = values.REGISTER_DATE; signDate.value = values.SIGN_DATE; productType.value = values.PRODUCT_TYPE;
        billNote.textContent = '';
      } catch (error) {
        billNote.textContent = error.message;
      }
    };
    billCode.addEventListener('change', loadBill);

    const sidePicker = picker('EXCEPTIONSITE_SIDE_CODE', 'EXCEPTIONSITE_SIDE', 'side_sites',
      () => ({ EXCEPTION_TYPE: values.EXCEPTION_TYPE || '', SITE_TYPE: state.meta.SITE_TYPE || '' }), onSidePicked);
    const deptPicker = picker('EXCEPTION_DEPT_SIDE_CODE', 'EXCEPTION_DEPT_SIDE', 'depts');
    const manPicker = picker('EXCEPTION_MAN_SIDE_CODE', 'EXCEPTION_MAN_SIDE', 'employees');
    const applyObject = () => {
      // changeMethod：1 投诉中心/网点、2 投诉部门、3 投诉人，只开放对应的被投诉方。
      const value = values.OBJECT_TYPE || '1';
      sidePicker.setDisabled(value !== '1' || !state.meta.SITE_TYPE);
      deptPicker.setDisabled(value !== '2');
      manPicker.setDisabled(value !== '3');
      setRequired('EXCEPTIONSITE_SIDE_CODE', value === '1' && Boolean(state.meta.SITE_TYPE));
      setRequired('EXCEPTION_DEPT_SIDE_CODE', value === '2');
      setRequired('EXCEPTION_MAN_SIDE_CODE', value === '3');
    };
    const objectBox = radios('OBJECT_TYPE', [['1', '投诉中心/网点'], ['2', '投诉部门'], ['3', '投诉人']], applyObject);
    row('OBJECT_TYPE', '投诉对象', objectBox);
    const processType = select('APPEAL_TYPE');
    processType.setAttribute('aria-label', '处理类型');
    row('APPEAL_TYPE', '处理类型', processType, { required: true });
    const category = select('CATEGORY');
    category.setAttribute('aria-label', '投诉类别');
    row('CATEGORY', '投诉类别', category, { required: true });
    const complaintType = select('EXCEPTION_TYPE');
    complaintType.setAttribute('aria-label', '投诉类型');
    row('EXCEPTION_TYPE', '投诉类型', complaintType, { required: true });
    const collar = select('COLLAR_NUMBER_LIST');
    collar.setAttribute('aria-label', '围板箱编号');
    row('COLLAR_NUMBER_LIST', '围板箱编号', collar, { required: true });
    show('COLLAR_NUMBER_LIST', false);
    row('EXCEPTIONSITE_SIDE_CODE', '被投诉网点', sidePicker);
    const rebind = el('div', 'complaint-rebind');
    rebind.hidden = true;
    body.append(rebind);
    row('EXCEPTION_DEPT_SIDE_CODE', '被投诉部门', deptPicker);
    row('EXCEPTION_MAN_SIDE_CODE', '被投诉人', manPicker);
    const sitePicker = picker('EXCEPTION_SITE_CODE', 'EXCEPTION_SITE', 'sites');
    const applyAgent = () => {
      // 是否投诉代登记：否 = 投诉网点为当前登录网点（不可改）；是 = 自己选投诉网点。
      const agent = values.BL_AGENT === '1';
      sitePicker.setDisabled(!agent);
      setRequired('EXCEPTION_SITE_CODE', agent);
      if (!agent) sitePicker.querySelector('input').value = editing ? (values.EXCEPTION_SITE || '') : '当前登录网点';
    };
    row('BL_AGENT', '是否投诉代登记', radios('BL_AGENT', [['1', '是'], ['0', '否']], applyAgent));
    const phone = text('EXCEPTION_MAN_PHONE');
    phone.setAttribute('aria-label', '联系电话');
    row('EXCEPTION_MAN_PHONE', '联系电话', phone, { required: true });
    row('EXCEPTION_SITE_CODE', '投诉网点', sitePicker);
    const deptName = text('DEPT_NAME', { readonly: true });
    row('DEPT_NAME', '处理部门', deptName);
    row('EXCEPTION_VOLUME', '索赔金额', text('EXCEPTION_VOLUME'));
    row('LOSS_NUMBER', '损失数量', text('LOSS_NUMBER'));
    row('LOSS_GOODS_NAME', '损失货物品名及规格', text('LOSS_GOODS_NAME'));
    row('COMPLAINANT_NAME', '投诉人姓名', text('COMPLAINANT_NAME'));
    row('GOODS_PRICE', '实际货物价值', text('GOODS_PRICE'));
    row('PRODUCT_TYPE', '产品类型', productType);
    const unfixed = select('UNFIXED_REASON', (spec.unfixed_reasons || []).map((value) => ({ value, label: value })));
    const applyFixed = () => {
      const damaged = (spec.damage_categories || []).includes(values.CATEGORY);
      show('IS_FIXED', damaged);
      setRequired('IS_FIXED', damaged);
      const needReason = damaged && values.IS_FIXED && values.IS_FIXED !== '1';
      show('UNFIXED_REASON', Boolean(needReason));
      setRequired('UNFIXED_REASON', Boolean(needReason));
      const other = needReason && values.UNFIXED_REASON === '其它';
      show('OTHER_REASON', Boolean(other));
      setRequired('OTHER_REASON', Boolean(other));
    };
    row('IS_FIXED', '是否修复', radios('IS_FIXED', [['1', '是'], ['0', '否']], applyFixed));
    unfixed.addEventListener('change', applyFixed);
    row('UNFIXED_REASON', '未修复原因', unfixed);
    const remark = text('REMARK', { multiline: true, placeholder: '事情经过和投诉内容在500字以内' });
    remark.setAttribute('aria-label', '问题描述');
    row('REMARK', '问题描述', remark);
    const typeText = text('EXCEPTION_TYPE_DATA_FROM', { readonly: true, multiline: true });
    row('EXCEPTION_TYPE_DATA_FROM', '类型描述', typeText);
    row('OTHER_REASON', '其它未修复原因', text('OTHER_REASON', { multiline: true }));

    // ---- 附件科目 ----
    const subjectsBox = el('div', 'complaint-subjects');
    body.append(subjectsBox);
    const paintSubjects = () => {
      subjectsBox.replaceChildren(el('div', 'complaint-subjects-title', '投诉方附件（按附件科目上传，单次最多 5 个）'));
      if (!state.subjects.length) { subjectsBox.append(el('p', 'action-site-note', '选择投诉类型后显示需要的附件科目。')); return; }
      state.subjects.forEach((subject) => {
        const box = el('div', 'complaint-subject');
        const titleLine = el('div', 'complaint-subject-head');
        titleLine.append(el('strong', '', subject.FILE_NAME_NEW || subject.FILE_CODE),
          el('span', subject.IS_REQUIRED_TEXT === '是' ? 'action-required' : 'action-site-note', subject.IS_REQUIRED_TEXT === '是' ? '必填' : '选填'));
        if (subject.FILE_DESCRIPTION) titleLine.append(el('span', 'action-site-note', subject.FILE_DESCRIPTION));
        box.append(titleLine);
        state.existing.filter((file) => file.FILE_CODE === subject.FILE_CODE && !state.removed.has(file.GUID)).forEach((file) => {
          const line = el('div', 'attach-item');
          const remove = el('button', 'attach-remove', '删除');
          remove.type = 'button';
          remove.addEventListener('click', () => { if (window.confirm('是否删除选中数据？')) { state.removed.add(file.GUID); paintSubjects(); } });
          line.append(el('span', 'attach-name', file.FILE_NAME), remove);
          box.append(line);
        });
        const files = el('div', 'action-files');
        state.files[subject.FILE_CODE] = state.files[subject.FILE_CODE] || [];
        window.OriginalPageAttachments?.render(files, { platform: 'ronghui', accountId: context.account_id, purpose: 'complaint_form' }, state.files[subject.FILE_CODE]);
        box.append(files);
        subjectsBox.append(box);
      });
    };

    // ---- 联动 ----
    const loadOptions = async (box, operation, payload) => {
      box.disabled = true;
      try {
        const data = await lookup(operation, payload);
        box.fill(data.options || []);
        box.optionsData = data.options || [];
      } catch (error) {
        box.fill([]);
        box.title = error.message;
      }
      box.disabled = false;
    };
    const applyType = async () => {
      const option = (complaintType.optionsData || []).find((item) => item.value === values.EXCEPTION_TYPE);
      state.meta = option?.meta || {};
      const meta = state.meta;
      // changeEXCEPTION_TYPE：围板箱、被投诉网点范围、仲裁类必填项、投诉内容必填、处理部门、类型描述、附件科目。
      const isCollar = values.EXCEPTION_TYPE === spec.collar_type;
      show('COLLAR_NUMBER_LIST', isCollar);
      if (isCollar) { values.OBJECT_TYPE = '1'; objectBox.setValue('1'); }
      objectBox.querySelectorAll('input').forEach((input) => { input.disabled = isCollar; });
      const arbitration = meta.BL_ARBITRATION && meta.BL_ARBITRATION !== '0';
      ['LOSS_GOODS_NAME', 'COMPLAINANT_NAME', 'GOODS_PRICE'].forEach((name) => setRequired(name, Boolean(arbitration)));
      setRequired('REMARK', meta.IS_COMPLAINT_CONTENT === '1');
      values.DEPT_NAME = meta.DEPT_NAME || '';
      deptName.value = values.DEPT_NAME;
      applyObject();
      state.subjects = [];
      if (meta.TYPE_CODE) {
        try { state.subjects = (await lookup('subjects', { TYPE_CODE: meta.TYPE_CODE })).subjects || []; } catch (error) { state.subjects = []; }
        try { values.EXCEPTION_TYPE_DATA_FROM = (await lookup('type_info', { COMPLAINT_TYPE: values.EXCEPTION_TYPE })).description || ''; } catch (error) { /* 读不到不影响保存 */ }
        typeText.value = values.EXCEPTION_TYPE_DATA_FROM || '';
      }
      paintSubjects();
    };
    processType.addEventListener('change', async () => {
      // xxxValueChange2：仲裁处理时索赔金额、损失数量必填；重读投诉类别。
      const arbitration = values.APPEAL_TYPE === '仲裁处理';
      setRequired('EXCEPTION_VOLUME', arbitration);
      setRequired('LOSS_NUMBER', arbitration);
      category.fill([]); complaintType.fill([]);
      if (values.APPEAL_TYPE) await loadOptions(category, 'categories', { APPEAL_TYPE: values.APPEAL_TYPE });
      category.dispatchEvent(new Event('change'));
    });
    category.addEventListener('change', async () => {
      applyFixed();
      complaintType.fill([]);
      if (values.CATEGORY) await loadOptions(complaintType, 'types', { APPEAL_TYPE: values.APPEAL_TYPE, CATEGORY: values.CATEGORY });
      await applyType();
    });
    complaintType.addEventListener('change', applyType);
    async function onSidePicked(site, note, input) {
      values.BL_BANGDING_SITE = '0';
      rebind.hidden = true;
      if (site.OPERATE_ATTR === '2') {
        // 原页：操作场不参与扣款时询问改绑网点。
        try {
          const data = await lookup('czc_site', { SITE_CODE: site.SITE_CODE });
          if (data.range_company && data.range_company_code) {
            rebind.replaceChildren(el('span', '', `该操作场不参与扣款，是否要改为绑定网点【${data.range_company}】进行投诉？`));
            const useDefault = el('button', 'action-button', '使用系统默认网点');
            const useManual = el('button', 'action-button', '使用人工填写网点');
            [useDefault, useManual].forEach((button) => { button.type = 'button'; });
            useDefault.addEventListener('click', () => {
              values.EXCEPTIONSITE_SIDE_CODE = data.range_company_code;
              values.EXCEPTIONSITE_SIDE = data.range_company;
              input.value = data.range_company;
              note.textContent = `已选：${data.range_company_code}`;
              values.BL_BANGDING_SITE = '1';
              rebind.hidden = true;
            });
            useManual.addEventListener('click', () => { values.BL_BANGDING_SITE = '2'; rebind.hidden = true; });
            rebind.append(useDefault, useManual);
            rebind.hidden = false;
          }
        } catch (error) { /* 读不到改绑网点时按原选择 */ }
      }
      if (values.EXCEPTION_TYPE === spec.collar_type) {
        await loadOptions(collar, 'collars', { SITE_CODE: values.EXCEPTIONSITE_SIDE_CODE });
      }
    }

    // ---- 保存 ----
    const foot = el('div', 'action-dialog-foot');
    const message = el('span', 'action-message');
    const save = el('button', 'action-save', '保存');
    save.type = 'button';
    const cancel = el('button', 'action-cancel', '取消');
    cancel.type = 'button';
    foot.append(message, save, cancel);
    const shut = () => { dialog.close(); dialog.remove(); };
    close.addEventListener('click', shut);
    cancel.addEventListener('click', shut);
    save.addEventListener('click', async () => {
      const missing = Object.entries(rows).some(([name, entry]) => !entry.line.hidden && !entry.star.hidden && !String(values[name] || '').trim());
      if (missing) { message.textContent = '请把信息填写完整'; message.dataset.tone = 'error'; return; }
      const payload = {};
      ['EXCEPTION_MAN_TYPE', 'BILL_CODE', 'OBJECT_TYPE', 'APPEAL_TYPE', 'CATEGORY', 'EXCEPTION_TYPE', 'COLLAR_NUMBER_LIST',
        'EXCEPTIONSITE_SIDE_CODE', 'EXCEPTIONSITE_SIDE', 'EXCEPTION_DEPT_SIDE_CODE', 'EXCEPTION_MAN_SIDE_CODE', 'BL_AGENT',
        'EXCEPTION_MAN_PHONE', 'EXCEPTION_SITE_CODE', 'EXCEPTION_VOLUME', 'LOSS_NUMBER', 'LOSS_GOODS_NAME', 'COMPLAINANT_NAME',
        'GOODS_PRICE', 'IS_FIXED', 'UNFIXED_REASON', 'REMARK', 'OTHER_REASON', 'BL_BANGDING_SITE'].forEach((key) => { payload[key] = String(values[key] || ''); });
      Object.entries(state.files).forEach(([code, items]) => { if (items.length) payload[`file__${code}`] = items; });
      if (state.removed.size) payload.remove_paths = [...state.removed].join(',');
      save.disabled = true; cancel.disabled = true;
      message.dataset.tone = ''; message.textContent = '保存中…';
      try {
        const data = await api.request('process', { ...context, kind: spec.kind, operation: spec.operation, payload }, requestId);
        message.textContent = '数据保存成功';
        onDone?.(spec, data);
        window.setTimeout(shut, 600);
      } catch (error) {
        if (!error.network) requestId = uuid();
        message.textContent = error.message; message.dataset.tone = 'error';
        save.disabled = false; cancel.disabled = false;
      }
    });

    // ---- 业务账号（新增挂在合并的分类上时先选账号） ----
    const top = el('div', '');
    if (Array.isArray(spec.account_options) && spec.account_options.length) {
      const account = el('select', 'action-select');
      account.setAttribute('aria-label', '业务账号');
      spec.account_options.forEach(([value, label]) => { const option = el('option', '', label); option.value = value; account.append(option); });
      account.value = context.account_id;
      context.account_id = account.value;
      account.addEventListener('change', () => { context.account_id = account.value; loadOptions(processType, 'process_types', {}); });
      const line = el('label', 'action-row');
      const caption = el('span', 'action-label');
      caption.append(el('span', 'action-required', '*'), document.createTextNode('业务账号'));
      line.append(caption, account);
      top.append(line);
    }
    dialog.append(head, top, body, foot);
    document.body.append(dialog);
    dialog.showModal();
    applyAgent();
    applyFixed();
    paintSubjects();
    (async () => {
      await loadOptions(processType, 'process_types', {});
      if (editing) {
        // 修改：按原记录依次读出投诉类别、投诉类型（setGUID），再读已有附件。
        if (values.APPEAL_TYPE) await loadOptions(category, 'categories', { APPEAL_TYPE: values.APPEAL_TYPE });
        if (values.CATEGORY) await loadOptions(complaintType, 'types', { APPEAL_TYPE: values.APPEAL_TYPE, CATEGORY: values.CATEGORY });
        const arbitration = values.APPEAL_TYPE === '仲裁处理';
        setRequired('EXCEPTION_VOLUME', arbitration);
        setRequired('LOSS_NUMBER', arbitration);
        try { state.existing = (await lookup('existing', { EXCEPTION_ID: values.EXCEPTION_ID })).files || []; } catch (error) { state.existing = []; }
        const description = values.EXCEPTION_TYPE_DATA_FROM;
        await applyType();
        values.EXCEPTION_TYPE_DATA_FROM = description;
        typeText.value = description || '';
        applyFixed();
      }
    })();
    return dialog;
  }

  window.ComplaintForm = { open };
})();
