/* Attachments picked and uploaded the way the original pages do it (2026-11-27). Each chosen file is
 * checked like the page checks it (count, size, 韵达 names and extensions, duplicates), 韵达 images over
 * 20KB are recompressed at quality 0.5 like the reply page, then uploaded at once through
 * /customer-service/problems/attachments/upload with the problem's business account; the list keeps
 * the descriptors the processing action sends back. Limits come from shared/original_page_attachments.py
 * (the server checks them again). Text only via textContent. */
(() => {
  const MB = 1024 * 1024;
  // (platform:purpose) -> [most files, largest file]; mirrors shared/original_page_attachments.LIMITS.
  const LIMITS = {
    'ronghui:work_order': [9, 200 * MB], 'ronghui:complaint_reply': [10, 20 * MB], 'ronghui:appeal': [20, 100 * MB],
    'ronghui:complaint_image': [1, 30 * MB], 'yunda:problem_reply': [3, 5 * MB], 'yunda:transfer': [3, 5 * MB]
  };
  const YUNDA_NAME = /^[a-zA-Z0-9\-._一-龥]+$/;
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const uuid = () => (window.crypto?.randomUUID ? window.crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

  function compressImage(file) {
    // 韵达回复页：图片超过 20KB 先按质量 0.5 重新压缩再上传。
    return new Promise((resolve) => {
      const reader = new FileReader();
      reader.onload = () => {
        const image = new Image();
        image.onload = () => {
          const canvas = document.createElement('canvas');
          canvas.width = image.width;
          canvas.height = image.height;
          canvas.getContext('2d').drawImage(image, 0, 0);
          canvas.toBlob((blob) => resolve(blob ? new File([blob], file.name, { type: 'image/jpeg' }) : file), 'image/jpeg', 0.5);
        };
        image.onerror = () => resolve(file);
        image.src = reader.result;
      };
      reader.onerror = () => resolve(file);
      reader.readAsDataURL(file);
    });
  }

  async function upload(file, context) {
    const body = new FormData();
    body.append('platform', context.platform);
    body.append('account_id', context.accountId);
    body.append('purpose', context.purpose);
    body.append('file', file, file.name);
    const fetcher = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
    const response = await fetcher('/customer-service/problems/attachments/upload', {
      method: 'POST', headers: { 'X-Browser-Request-UUID': uuid() }, body
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.ok === false || !data.attachment) throw new Error(data.message || `上传失败（${response.status}）`);
    return data.attachment;
  }

  /** Render the picker into ``container``. ``context``: platform, accountId, purpose; ``items`` is the
   *  array of uploaded descriptors (kept by the caller's draft). */
  function render(container, context, items) {
    const [most, largest] = LIMITS[`${context.platform}:${context.purpose}`] || [3, 5 * MB];
    container.replaceChildren();
    const box = el('div', 'attach-box');
    const zone = el('label', 'attach-zone', `附件选择（最多${most}个，最大${Math.round(largest / MB)}M，支持拖拽）`);
    const input = el('input');
    input.type = 'file';
    input.multiple = true;
    input.hidden = true;
    zone.append(input);
    const message = el('div', 'attach-message');
    const list = el('ul', 'attach-list');
    const redraw = () => render(container, context, items);
    items.forEach((item, index) => {
      const row = el('li', 'attach-item');
      row.append(el('span', 'attach-name', item.file_name || item.old_name || '附件'));
      const remove = el('button', 'attach-remove', '删除');
      remove.type = 'button';
      remove.addEventListener('click', () => { items.splice(index, 1); redraw(); });
      row.append(remove);
      list.append(row);
    });
    async function take(files) {
      const chosen = Array.from(files || []);
      if (items.length + chosen.length > most) { message.textContent = `最多只能上传${most}个文件`; return; }
      for (const file of chosen) {
        if (file.size > largest) { message.textContent = `单个文件最大${Math.round(largest / MB)}Mb`; return; }
        if (context.platform === 'yunda' && !YUNDA_NAME.test(file.name)) { message.textContent = '不支持文件名包含符号的文件'; return; }
        if (items.some((item) => (item.file_name || item.old_name) === file.name)) { message.textContent = `文件 ${file.name} 已存在`; return; }
      }
      for (const file of chosen) {
        message.textContent = `正在上传 ${file.name}…`;
        try {
          const ready = context.platform === 'yunda' && /^image\//.test(file.type) && file.size > 20000 ? await compressImage(file) : file;
          items.push(await upload(ready, context));
        } catch (error) {
          message.textContent = `${file.name}：${error.message || '上传失败'}`;
          redraw();
          container.querySelector('.attach-message').textContent = `${file.name}：${error.message || '上传失败'}`;
          return;
        }
      }
      redraw();
    }
    input.addEventListener('change', () => take(input.files));
    zone.addEventListener('dragover', (event) => { event.preventDefault(); zone.classList.add('is-over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('is-over'));
    zone.addEventListener('drop', (event) => { event.preventDefault(); zone.classList.remove('is-over'); take(event.dataTransfer?.files); });
    box.append(zone, message, list);
    container.append(box);
  }

  window.OriginalPageAttachments = { render, LIMITS };
})();
