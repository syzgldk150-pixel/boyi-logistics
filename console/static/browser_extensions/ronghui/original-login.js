(() => {
  const BOYI = 'https://boyi.homes';
  const YUNDA = 'https://kyinms.yunda56.com';
  const YUNDA_SSO = 'https://ky-sso.yunda56.com';
  const YUNDA_HOME = '/ky_inms/public/index.php/index/index.html';
  const YUNDA_ENTRY = '/ky_inms/public/index.php/business/waybill/entry/indexNew.html';
  if (location.origin === BOYI && window.parent === window) {
    // The SSO page tries to navigate window.top, which the embed must block.
    // Show a real login action instead of an embedded QR that cannot finish.
    window.addEventListener('message', event => {
      if (event.origin !== YUNDA_SSO || event.data?.type !== 'boyi-yunda-login-required') return;
      const frame = Array.from(document.querySelectorAll('iframe[data-yunda-live-frame]'))
        .find(item => item.contentWindow === event.source);
      const panel = frame?.closest('[data-entry-frame-panel][data-entry-provider="yunda"]');
      const link = panel?.querySelector('[data-entry-login="yunda"]');
      if (!link || panel.querySelector('[data-yunda-login-required]')) return;
      const prompt = document.createElement('div');
      prompt.dataset.yundaLoginRequired = '';
      prompt.style.cssText = 'padding:32px 24px;color:var(--text,#374151)';
      const title = document.createElement('h3');
      title.textContent = '登录韵达后继续录单';
      title.style.cssText = 'margin:0 0 8px;font-size:18px;color:var(--ink,#111)';
      const description = document.createElement('p');
      description.textContent = '请在新窗口扫码登录，成功后会自动回到此录单页。';
      description.style.cssText = 'margin:0 0 20px;line-height:1.6';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'primary-btn';
      button.textContent = '打开韵达扫码登录';
      button.addEventListener('click', () => link.click());
      prompt.append(title, description, button);
      frame.style.display = 'none';
      frame.parentElement.append(prompt);
      // The existing helper return and manual reload share this exact frame.
      frame.addEventListener('console:original-page-reload', () => {
        prompt.remove();
        frame.style.removeProperty('display');
      }, {once:true});
    });
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
  if (location.origin === YUNDA_SSO && location.pathname === '/login') {
    if (window.parent !== window && location.ancestorOrigins?.[0] === BOYI) {
      window.parent.postMessage({type:'boyi-yunda-login-required'}, BOYI);
    }
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
