(() => {
  const BOYI = 'https://boyi.homes';
  const YUNDA = 'https://kyinms.yunda56.com';
  const YUNDA_SSO = 'https://ky-sso.yunda56.com';
  const YUNDA_HOME = '/ky_inms/public/index.php/index/index.html';
  const YUNDA_ENTRY = '/ky_inms/public/index.php/business/waybill/entry/indexNew.html';
  const BEST = 'https://v5.800best.com';
  const BEST_ENTRY = '/baseService/transOrder/createOrder';
  if (location.origin === BOYI && window.parent === window) {
    window.addEventListener('message', event => {
      if (event.origin !== BEST || !['boyi-best-entry-unavailable', 'boyi-best-storage-unavailable'].includes(event.data?.type)) return;
      const frame = Array.from(document.querySelectorAll('iframe[data-best-live-frame]'))
        .find(item => item.contentWindow === event.source);
      const panel = frame?.closest('[data-entry-frame-panel][data-entry-provider="best"]');
      const notice = panel?.querySelector('[data-best-live-fallback]');
      if (!notice) return;
      notice.textContent = event.data.type === 'boyi-best-storage-unavailable' ?
        '浏览器未允许百世内嵌页使用原站登录状态。请允许此网站的第三方网站数据后重新加载，或在新窗口录单。' :
        '未能自动进入百世录入表单，请点击原页的“运单录入”页签，或重新加载。';
      notice.hidden = false;
    });
    // The SSO page tries to navigate window.top, which the embed must block.
    // Show a real login action instead of an embedded QR that cannot finish.
    window.addEventListener('message', event => {
      const provider = event.origin === YUNDA_SSO && event.data?.type === 'boyi-yunda-login-required' ? 'yunda' :
        event.origin === BEST && event.data?.type === 'boyi-best-login-required' ? 'best' : null;
      if (!provider) return;
      const label = provider === 'best' ? '百世' : '韵达';
      const frame = Array.from(document.querySelectorAll(`iframe[data-${provider}-live-frame]`))
        .find(item => item.contentWindow === event.source);
      const panel = frame?.closest(`[data-entry-frame-panel][data-entry-provider="${provider}"]`);
      const link = panel?.querySelector(`[data-entry-login="${provider}"]`);
      if (!link || panel.querySelector(`[data-${provider}-login-required]`)) return;
      const prompt = document.createElement('div');
      prompt.setAttribute(`data-${provider}-login-required`, '');
      prompt.style.cssText = 'padding:32px 24px;color:var(--text,#374151)';
      const title = document.createElement('h3');
      title.textContent = `登录${label}后继续录单`;
      title.style.cssText = 'margin:0 0 8px;font-size:18px;color:var(--ink,#111)';
      const description = document.createElement('p');
      description.textContent = '请在新窗口扫码登录，成功后会自动回到此录单页。';
      description.style.cssText = 'margin:0 0 20px;line-height:1.6';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'primary-btn';
      button.textContent = `打开${label}扫码登录`;
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
      if (!['yunda', 'ronghui', 'best'].includes(provider)) return;
      const source = new URL(link.href);
      const expected = {yunda:[YUNDA, YUNDA_ENTRY], best:[BEST, BEST_ENTRY],
        ronghui:['https://tms.ronghuiwl.com', '/module/index']}[provider];
      if (source.origin !== expected[0] || source.pathname !== expected[1]) return;
      event.preventDefault();
      panel.dataset.entryAwaitingLogin = '1';
      try {
        const result = await chrome.runtime.sendMessage({type:'open-original-login',
          provider, entryId:panel.dataset.entryId});
        if (!result?.ok) throw new Error('Login window unavailable');
      } catch (error) {
        delete panel.dataset.entryAwaitingLogin;
        panel.querySelector('.entry-origin-notice p').textContent =
          String(error?.message).includes('Extension context invalidated') ?
            '原页助手已重新加载，当前网页连接已失效。请先保存其他页签的内容，再刷新整个博益网页后重试。' :
            '登录窗口未能打开，请确认原页助手已启用；若刚更新过扩展，请先保存内容并刷新整个博益网页，或右键在新窗口打开原站链接。';
      }
    });
    chrome.runtime.onMessage.addListener((message, sender, reply) => {
      if (message?.type !== 'original-login-complete') return;
      const panel = Array.from(document.querySelectorAll('[data-entry-frame-panel]'))
        .find(item => item.dataset.entryId === message.entryId &&
          item.dataset.entryProvider === message.provider && item.dataset.entryAwaitingLogin === '1');
      if (!panel) { reply({ok:false}); return; }
      delete panel.dataset.entryAwaitingLogin;
      const tab = Array.from(document.querySelectorAll('[data-entry-tab]'))
        .find(item => item.dataset.entryId === message.entryId && item.dataset.entryProvider === message.provider);
      tab?.querySelector('[data-entry-activate]')?.click();
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
  if (location.origin === BEST) {
    // BEST renders cached tabs before its server rejects an expired session.
    // Keep observing its SPA login/entry transitions; a tab label is not proof.
    let path, generation = 0, attempted = false, complete = false;
    async function checkBest() {
      if (complete) return;
      if (path !== location.pathname) {
        path = location.pathname;
        generation++;
        attempted = false;
        if (path === '/login' && window.parent !== window && location.ancestorOrigins?.[0] === BOYI) {
          window.parent.postMessage({type:'boyi-best-login-required'}, BOYI);
        }
      }
      if (window.parent !== window || path !== BEST_ENTRY || attempted ||
          !Array.from(document.querySelectorAll('[role="tab"]'))
            .some(tab => tab.textContent.trim() === '运单录入')) return;
      attempted = true;
      const checkedGeneration = generation;
      try {
        const response = await fetch('/ltlv5-war/web/menu/getUserMenuVos', {
          credentials:'include', cache:'no-store', signal:AbortSignal.timeout(10000)
        });
        if (!response.ok) return;
        const payload = await response.json();
        if (String(payload?.code) !== '200' || !Array.isArray(payload?.vo?.menuTreeNode?.children) ||
            checkedGeneration !== generation || location.pathname !== BEST_ENTRY) return;
        const result = await chrome.runtime.sendMessage({type:'original-login-ready', provider:'best'});
        if (result?.ok) { complete = true; observer.disconnect(); }
      } catch {
        // Failed verification keeps the native login window open. No credentials
        // or response bodies are retained, logged or sent to the Console.
      }
    }
    const observer = new MutationObserver(checkBest);
    observer.observe(document, {subtree:true, childList:true});
    window.addEventListener('popstate', checkBest);
    // React may update the DOM before pushState/replaceState. Those navigations
    // do not emit popstate and must reset the previous verification attempt.
    window.navigation?.addEventListener('navigatesuccess', checkBest);
    checkBest();
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
