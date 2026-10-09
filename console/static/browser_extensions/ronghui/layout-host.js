(() => {
  if (location.origin !== 'https://boyi.homes' ||
      window.__boyiRonghuiLayoutHost) return;
  window.__boyiRonghuiLayoutHost = true;
  const providers = [
    {id:'ronghui', label:'融辉', origin:'https://tms.ronghuiwl.com'},
    {id:'best', label:'百世', origin:'https://v5.800best.com'}
  ];
  const mounted = new WeakSet();
  function query(frame, provider) {
    if (!frame.getAttribute('src') || frame.contentDocument) return;
    frame.contentWindow?.postMessage({type: `boyi-${provider.id}-layout-query`}, provider.origin);
  }
  function scan() {
    providers.forEach(provider => document.querySelectorAll(`iframe[data-${provider.id}-live-frame]`).forEach(frame => {
      if (mounted.has(frame)) return;
      mounted.add(frame);
      frame.addEventListener('load', () => query(frame, provider));
      query(frame, provider);
    }));
  }
  window.addEventListener('message', event => {
    const state = event.data;
    const provider = providers.find(item => event.origin === item.origin && state?.type === `boyi-${item.id}-layout-state`);
    if (!provider ||
        typeof state.available !== 'boolean' || typeof state.focused !== 'boolean') return;
    const frame = Array.from(document.querySelectorAll(`iframe[data-${provider.id}-live-frame]`))
      .find(item => item.contentWindow === event.source);
    const notice = frame?.closest(`[data-${provider.id}-root]`)?.querySelector('.entry-origin-notice');
    if (!notice) return;
    let button = notice.querySelector(`[data-${provider.id}-layout-toggle]`);
    if (!button) {
      button = document.createElement('button');
      button.type = 'button';
      button.className = 'ghost-btn';
      button.setAttribute(`data-${provider.id}-layout-toggle`, '');
      button.addEventListener('click', () => {
        frame.contentWindow?.postMessage({
          type: `boyi-${provider.id}-layout-set`, focused: button.dataset.focused !== 'true'
        }, provider.origin);
      });
      notice.insertBefore(button, notice.querySelector('a'));
    }
    button.hidden = !state.available;
    button.dataset.focused = String(state.focused);
    button.textContent = state.focused ? '原站菜单' : '仅显示录单';
    button.setAttribute('aria-label', state.focused ? `显示${provider.label}原站菜单` : `仅显示${provider.label}运单录入页`);
    const description = notice.querySelector('p');
    if (description) description.textContent = state.focused
      ? `已展开${provider.label}运单录入，可通过“原站菜单”切换账号或使用其他功能。`
      : (provider.id === 'best' ? '已显示百世原站菜单；可点“仅显示录单”收起顶部主导航。'
        : '登录后自动进入运单录入；可通过原站菜单切换其他功能。');
  });
  new MutationObserver(scan).observe(document, {subtree: true, childList: true});
  scan();
})();
