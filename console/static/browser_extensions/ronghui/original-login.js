(() => {
  const BOYI = 'https://boyi.homes';
  const YUNDA = 'https://kyinms.yunda56.com';
  const YUNDA_HOME = '/ky_inms/public/index.php/index/index.html';
  const YUNDA_ENTRY = '/ky_inms/public/index.php/business/waybill/entry/indexNew.html';
  if (location.origin === BOYI && window.parent === window) {
    document.addEventListener('click', async event => {
      const link = event.target.closest('.entry-origin-notice a[target="_blank"]');
      if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const panel = link.closest('[data-entry-frame-panel]');
      const provider = panel?.dataset.entryProvider;
      if (!['yunda', 'ronghui'].includes(provider)) return;
      const source = new URL(link.href);
      if (provider === 'yunda' ? source.origin !== YUNDA || source.pathname !== YUNDA_ENTRY :
        source.origin !== 'https://tms.ronghuiwl.com' || source.pathname !== '/module/index') return;
      event.preventDefault();
      panel.dataset.entryAwaitingLogin = '1';
      try {
        const result = await chrome.runtime.sendMessage({type:'open-original-login',
          provider, entryId:panel.dataset.entryId});
        if (!result?.ok) throw new Error('Login window unavailable');
      } catch {
        delete panel.dataset.entryAwaitingLogin;
        panel.querySelector('.entry-origin-notice p').textContent =
          '登录窗口未能打开，请更新原页助手后重试，或右键在新窗口打开原站链接。';
      }
    });
    chrome.runtime.onMessage.addListener((message, sender, reply) => {
      if (message?.type !== 'original-login-complete') return;
      const panel = Array.from(document.querySelectorAll('[data-entry-frame-panel]'))
        .find(item => item.dataset.entryId === message.entryId &&
          item.dataset.entryProvider === message.provider && item.dataset.entryAwaitingLogin === '1');
      if (!panel) { reply({ok:false}); return; }
      delete panel.dataset.entryAwaitingLogin;
      panel.querySelector('[data-entry-reload]').click();
      reply({ok:true});
    });
    return;
  }
  const ronghui = location.origin === 'https://tms.ronghuiwl.com' && location.pathname === '/module/index';
  const yunda = location.origin === YUNDA && [YUNDA_HOME, YUNDA_ENTRY].includes(location.pathname);
  if (!ronghui && !yunda) return;
  let complete = false;
  function check() {
    const ready = ronghui ?
      (document.querySelector('#mainTabs') && document.querySelector('#mainMenu a.menu-title')) :
      (location.pathname === YUNDA_HOME ? document.querySelector('#admin-navbar-side') :
        document.querySelector('[name="LogisticsId"]'));
    if (!ready || complete) return;
    complete = true;
    observer.disconnect();
    if (window.parent === window) {
      // Only the background's explicitly opened login tab can complete a return.
      chrome.runtime.sendMessage({type:'original-login-ready', provider:ronghui?'ronghui':'yunda'}).catch(() => {});
    } else if (yunda && location.pathname === YUNDA_HOME && location.ancestorOrigins?.[0] === BOYI) {
      location.replace(YUNDA + YUNDA_ENTRY + '?page=tab&p=nil');
    }
  }
  const observer = new MutationObserver(check);
  observer.observe(document, {subtree:true, childList:true});
  check();
})();
