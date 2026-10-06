(() => {
  if (location.origin !== 'https://boyi.homes' ||
      window.__boyiRonghuiLayoutHost) return;
  window.__boyiRonghuiLayoutHost = true;
  const ORIGIN = 'https://tms.ronghuiwl.com';
  const mounted = new WeakSet();
  function query(frame) {
    frame.contentWindow?.postMessage({type: 'boyi-ronghui-layout-query'}, ORIGIN);
  }
  function scan() {
    document.querySelectorAll('iframe[data-ronghui-live-frame]').forEach(frame => {
      if (mounted.has(frame)) return;
      mounted.add(frame);
      frame.addEventListener('load', () => query(frame));
      query(frame);
    });
  }
  window.addEventListener('message', event => {
    const state = event.data;
    if (event.origin !== ORIGIN || state?.type !== 'boyi-ronghui-layout-state' ||
        typeof state.available !== 'boolean' || typeof state.focused !== 'boolean') return;
    const frame = Array.from(document.querySelectorAll('iframe[data-ronghui-live-frame]'))
      .find(item => item.contentWindow === event.source);
    const notice = frame?.closest('[data-ronghui-root]')?.querySelector('.entry-origin-notice');
    if (!notice) return;
    let button = notice.querySelector('[data-ronghui-layout-toggle]');
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'ghost-btn';
      button.dataset.ronghuiLayoutToggle = '';
      button.addEventListener('click', () => {
        frame.contentWindow?.postMessage({
          type: 'boyi-ronghui-layout-set', focused: button.dataset.focused !== 'true'
        }, ORIGIN);
      });
      notice.insertBefore(button, notice.querySelector('a'));
    }
    button.hidden = !state.available;
    button.dataset.focused = String(state.focused);
    button.textContent = state.focused ? '原站菜单' : '仅显示录单';
    button.setAttribute('aria-label', state.focused ? '显示融辉原站菜单' : '仅显示融辉运单录入页');
    const description = notice.querySelector('p');
    if (description) description.textContent = state.focused
      ? '已展开融辉运单录入，可通过“原站菜单”切换账号或使用其他功能。'
      : '从融辉菜单进入“运单录入”后，自动展开录单区域。';
  });
  new MutationObserver(scan).observe(document, {subtree: true, childList: true});
  scan();
})();
