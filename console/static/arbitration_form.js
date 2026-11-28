/* 韵达快运仲裁“继续申报”，按原页仲裁查询详情的可改字段与规则（2026-11-28）：仲裁类型、仲裁类别（自动仲裁类别、
 * 费用申请仲裁按原页不可选）、索赔金额、责任网点（不是运单关联网点时提示）、异常件数、问题描述（1000 字内）；
 * 换类别时按类别配置显示主面单/价值证明/索赔函/举证图片分组，换到转退件费时先问“是否地址解析问题”；已有视频时
 * 须保留 1–3 段，录音最多 2 条；已有附件可删除。数据来自原平台只读查询（/customer-service/lookup），检查、写入与
 * 回读在 Agent（shared/original_page_actions.py）。Text only via textContent. */
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
    const lookup = (operation, payload = {}) => api.request('lookup', { ...context, kind: 'arbitration_form', operation, payload });
    const values = {};
    const state = { files: {}, removed: new Set(), uploads: {}, visible: {}, original: {}, waybillDepts: [], isGis: 'T' };
    let requestId = uuid();

    const dialog = el('dialog', 'action-dialog complaint-dialog');
    dialog.setAttribute('aria-label', '继续申报');
    const head = el('div', 'action-dialog-head');
    head.append(el('h3', '', '继续申报'));
    const close = el('button', 'action-dialog-close', '×');
    close.type = 'button';
    close.setAttribute('aria-label', '关闭');
    head.append(close);
    const body = el('div', 'action-dialog-body complaint-body');
    body.append(el('p', 'action-site-note', '正在读取申报信息…'));
    const foot = el('div', 'action-dialog-foot');
    const message = el('span', 'action-message');
    const save = el('button', 'action-save', '保存');
    save.type = 'button';
    save.disabled = true;
    const cancel = el('button', 'action-cancel', '取消');
    cancel.type = 'button';
    foot.append(message, save, cancel);
    dialog.append(head, body, foot);
    const shut = () => { dialog.close(); dialog.remove(); };
    close.addEventListener('click', shut);
    cancel.addEventListener('click', shut);
    document.body.append(dialog);
    dialog.showModal();

    const row = (label, control, required = false) => {
      const line = el('label', 'action-row');
      const caption = el('span', 'action-label');
      if (required) caption.append(el('span', 'action-required', '*'));
      caption.append(document.createTextNode(label));
      line.append(caption, control);
      body.append(line);
      return line;
    };
    const input = (name, label, multiline = false, readonly = false) => {
      const box = el(multiline ? 'textarea' : 'input', readonly ? 'action-readonly' : (multiline ? 'action-textarea' : 'action-input'));
      if (!multiline) box.type = 'text';
      box.readOnly = readonly;
      box.value = values[name] || '';
      box.setAttribute('aria-label', label);
      box.addEventListener('input', () => { values[name] = box.value; });
      return box;
    };
    const select = (name, label) => {
      const box = el('select', 'action-select');
      box.setAttribute('aria-label', label);
      box.fill = (options, disabled = () => false) => {
        box.replaceChildren();
        options.forEach((option) => {
          const node = el('option', '', option.label);
          node.value = option.value;
          node.disabled = disabled(option.value);
          box.append(node);
        });
        box.value = values[name] || '';
        if (box.value !== values[name]) values[name] = box.value;
      };
      box.addEventListener('change', () => { values[name] = box.value; });
      return box;
    };

    const groupsBox = el('div', 'complaint-subjects');
    const paintGroups = () => {
      groupsBox.replaceChildren(el('div', 'complaint-subjects-title', '附件'));
      const section = (key, label, purpose, limit) => {
        const box = el('div', 'complaint-subject');
        box.dataset.group = key;
        box.append(el('strong', '', label));
        (state.files[key] || []).filter((file) => !state.removed.has(`old${file.fileId}`)).forEach((file) => {
          const line = el('div', 'attach-item');
          const remove = el('button', 'attach-remove', '删除');
          remove.type = 'button';
          remove.addEventListener('click', () => {
            if (window.confirm(`确定要删除选中的${purpose === 'arbitration_video' ? '视频' : purpose === 'arbitration_audio' ? '录音' : '图片'}？`)) {
              state.removed.add(`old${file.fileId}`);
              paintGroups();
            }
          });
          line.append(el('span', 'attach-name', file.fileName + (file.isCentreUpload === 'Y' ? '（中心上传）' : '')), remove);
          box.append(line);
        });
        const uploader = el('div', 'action-files');
        state.uploads[key] = state.uploads[key] || [];
        window.OriginalPageAttachments?.render(uploader, { platform: 'yunda', accountId: context.account_id, purpose }, state.uploads[key]);
        box.append(uploader);
        if (limit) box.append(el('span', 'action-site-note', `本次最多上传 ${limit} 个`));
        groupsBox.append(box);
      };
      (spec.picture_groups || []).forEach((group) => { if (state.visible[group.key]) section(group.key, `${group.label}上传`, 'arbitration_image', group.limit); });
      if ((state.files.video || []).length) section('video', '视频上传（最大30M，最多三段）', 'arbitration_video', 3);
      if ((state.files.audio || []).length) section('audio', '附件录音上传（mp3、m4a、wav，最多2条，单个不超过10M）', 'arbitration_audio', 2);
    };

    const build = async () => {
      const detail = await lookup('detail');
      Object.assign(values, detail.values || {});
      Object.assign(state.original, detail.values || {});
      state.files = detail.files || {};
      state.waybillDepts = detail.waybill_depts || [];
      (spec.picture_groups || []).forEach((group) => { state.visible[group.key] = Boolean((state.files[group.key] || []).length); });
      body.replaceChildren();
      row('运单号', input('waybillCode', '运单号', false, true));
      row('子单数', input('number', '子单数', false, true));
      const declType = select('declType', '仲裁类型');
      row('仲裁类型', declType, true);
      const declCategory = select('declCategory', '仲裁类别');
      row('仲裁类别', declCategory, true);
      const gisLine = row('是否地址解析问题', (() => {
        const box = el('select', 'action-select');
        box.setAttribute('aria-label', '是否地址解析问题');
        [['T', '是'], ['F', '否']].forEach(([value, label]) => { const option = el('option', '', label); option.value = value; box.append(option); });
        box.addEventListener('change', () => { state.isGis = box.value; });
        return box;
      })());
      gisLine.hidden = true;
      row('索赔金额', input('claimAmount', '索赔金额'), true);
      const dutyBox = el('div', 'action-site');
      const dutyInput = el('input', 'action-site-input');
      dutyInput.type = 'text';
      dutyInput.value = values.dutyDept || '';
      dutyInput.setAttribute('aria-label', '责任网点');
      const dutySearch = el('button', 'action-site-search', '查询');
      dutySearch.type = 'button';
      const dutyNote = el('span', 'action-site-note');
      const dutyList = el('ul', 'action-site-list');
      dutySearch.addEventListener('click', async () => {
        dutyNote.textContent = '查询中…';
        dutyList.replaceChildren();
        try {
          const data = await lookup('duty_depts', { key: dutyInput.value.trim() });
          dutyNote.textContent = (data.sites || []).length ? '' : '没有找到';
          (data.sites || []).forEach((site) => {
            const item = el('li');
            const pick = el('button', 'action-site-option', `${site.SITE_CODE} ${site.SITE_NAME}`);
            pick.type = 'button';
            pick.addEventListener('click', () => {
              values.dutyDept = site.SITE_NAME;
              values.dutyDeptCode = site.SITE_CODE;
              dutyInput.value = site.SITE_NAME;
              dutyList.replaceChildren();
              // 原页：选了不在运单关联网点里的网点时提示（不拦）。
              dutyNote.textContent = state.waybillDepts.length && !state.waybillDepts.includes(site.SITE_CODE)
                ? '此网点非该运单关联网点，请确认是否有误' : `已选：${site.SITE_CODE}`;
            });
            item.append(pick);
            dutyList.append(item);
          });
        } catch (error) { dutyNote.textContent = error.message; }
      });
      dutyInput.addEventListener('input', () => { values.dutyDeptCode = ''; });
      dutyBox.append(dutyInput, dutySearch, dutyNote, dutyList);
      row('责任网点', dutyBox, true);
      row('异常件数', input('abnormalNum', '异常件数'));
      row('问题描述', input('problemDescribe', '问题描述', true), true);
      const typeDescribe = input('typeDescribe', '类型描述', true, true);
      row('类型描述', typeDescribe);
      body.append(groupsBox);
      paintGroups();

      const types = (await lookup('types')).options || [];
      const currentType = state.original.declType;
      // loadDelongType('belongCate')：当前是费用申请仲裁时只能选它，否则它不可选。
      declType.fill(types, (value) => (currentType === spec.fee_apply ? value !== spec.fee_apply : value === spec.fee_apply));
      const loadCategories = async () => {
        const options = (await lookup('categories', { declType: values.declType })).options || [];
        declCategory.fill(options, (value) => value !== state.original.declCategory && (spec.auto_categories || []).includes(value));
      };
      const applyCategory = async () => {
        gisLine.hidden = !(values.declCategory === spec.gis_category && state.original.declCategory !== spec.gis_category);
        if (values.declCategory && values.declCategory !== state.original.declCategory) {
          const modules = (await lookup('modules', { declCategory: values.declCategory })).modules || {};
          (spec.picture_groups || []).forEach((group) => {
            const flag = { marina: modules.isBillsUpload, prove: modules.isClaimLetterPicture, state: modules.isStatement, proof: modules.isProofPicture }[group.key];
            if (flag === 'Y') state.visible[group.key] = true;
            if (flag === 'N') state.visible[group.key] = false;
          });
          values.typeDescribe = modules.remark || '';
          typeDescribe.value = values.typeDescribe;
        }
        paintGroups();
      };
      declType.addEventListener('change', async () => { await loadCategories(); await applyCategory(); });
      declCategory.addEventListener('change', applyCategory);
      await loadCategories();
      save.disabled = false;
    };

    const submit = async (gisConfirmed) => {
      const payload = {};
      ['declType', 'declCategory', 'claimAmount', 'dutyDept', 'dutyDeptCode', 'abnormalNum', 'problemDescribe'].forEach((key) => { payload[key] = String(values[key] || ''); });
      Object.entries(state.uploads).forEach(([key, items]) => { if (items.length) payload[`files_${key}`] = items; });
      if (state.removed.size) payload.remove_files = [...state.removed].join(',');
      payload.isGis = state.isGis;
      if (gisConfirmed) payload.gis_confirmed = '1';
      save.disabled = true; cancel.disabled = true;
      message.dataset.tone = ''; message.textContent = '保存中…';
      try {
        const data = await api.request('process', { ...context, kind: spec.kind, operation: spec.operation, payload }, requestId);
        message.textContent = '操作成功!';
        onDone?.(spec, data);
        window.setTimeout(shut, 600);
      } catch (error) {
        if (!error.network) requestId = uuid();
        save.disabled = false; cancel.disabled = false;
        if (error.code === 'GIS_CONFIRM_REQUIRED' && !gisConfirmed) {
          // 原页 checkGis：不是地址解析问题时先确认是否仍然申报。
          if (window.confirm(error.message)) { submit(true); return; }
          message.textContent = '已取消申报';
          return;
        }
        message.textContent = error.message; message.dataset.tone = 'error';
      }
    };
    save.addEventListener('click', () => { if (window.confirm('是否保存？')) submit(false); });
    build().catch((error) => { body.replaceChildren(el('p', 'action-message', error.message)); });
    return dialog;
  }

  window.ArbitrationForm = { open };
})();
