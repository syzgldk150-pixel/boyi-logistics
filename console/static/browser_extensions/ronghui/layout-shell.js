(() => {
  const BOYI = 'https://boyi.homes';
  if (location.origin !== 'https://tms.ronghuiwl.com' ||
      location.pathname !== '/module/index' || window.parent === window ||
      location.ancestorOrigins?.[0] !== BOYI || window.__boyiRonghuiLayout) return;
  window.__boyiRonghuiLayout = true;
  const root = document.documentElement;
  const style = document.createElement('style');
  style.id = 'boyi-ronghui-entry-layout';
  style.textContent = `
    html.boyi-entry-focus body > .navbar,
    html.boyi-entry-focus .container > .sidebar,
    html.boyi-entry-focus .main > .allMenu,
    html.boyi-entry-focus .main > .menu1,
    html.boyi-entry-focus .main > .mini-fit > .fullSm,
    html.boyi-entry-focus #mainTabs .mini-tabs-headers { display: none !important; }
    html.boyi-entry-focus .container > .main {
      position: absolute !important; inset: 0 !important;
      width: 100% !important; height: 100% !important; margin: 0 !important;
    }
    html.boyi-entry-focus .main > .mini-fit,
    html.boyi-entry-focus #mainTabs {
      width: 100% !important; height: 100% !important;
      margin: 0 !important; padding: 0 !important; box-sizing: border-box !important;
    }
    html.boyi-entry-focus #mainTabs > .mini-tabs-table { width: 100% !important; height: 100% !important; }
    html.boyi-entry-focus #mainTabs .mini-tabs-bodys {
      position: absolute !important; inset: 0 !important;
      width: auto !important; height: auto !important;
      padding: 0 !important; border: 0 !important;
    }
    html.boyi-entry-focus #mainTabs .mini-tabs-body {
      width: 100% !important; height: 100% !important;
      padding: 0 !important; border: 0 !important;
    }
    html.boyi-entry-focus #mainTabs .mini-tabs-body > iframe {
      display: block; width: 100% !important; height: 100% !important; border: 0;
    }
  `;
  (document.head || root).append(style);
  let preferred = true;
  let scheduled = false;
  let previousState = '';
  let entryOpened = false;
  function sync(force = false) {
    const tabs = window.mini?.get('mainTabs');
    if (tabs && !entryOpened) {
      const entries = tabs.getTabs().filter(tab => tab.title === '运单录入');
      const menus = Array.from(document.querySelectorAll('#mainMenu a.menu-title'))
        .filter(item => !item.closest('.compact-menu') && item.textContent.trim() === '运单录入');
      if (entries.length === 1) {
        entryOpened = true;
        if (tabs.getActiveTab() !== entries[0]) tabs.activeTab(entries[0]);
      } else if (entries.length === 0 && menus.length === 1) {
        // Use the authenticated native menu once; its URL carries transient state.
        entryOpened = true;
        menus[0].click();
      }
    }
    const entry = tabs?.getTabs().find(tab => tab.title === '运单录入');
    const available = Boolean(entry);
    const focused = available && tabs.getActiveTab() === entry && preferred;
    if (root.classList.contains('boyi-entry-focus') !== focused) {
      root.classList.toggle('boyi-entry-focus', focused);
      // Reflow the existing widgets; never replace or navigate the entry iframe.
      window.dispatchEvent(new Event('resize'));
    }
    const state = JSON.stringify({available, focused});
    if (force || state !== previousState) {
      previousState = state;
      window.parent.postMessage({type: 'boyi-ronghui-layout-state', available, focused}, BOYI);
    }
  }
  function queue() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; sync(); });
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== BOYI) return;
    if (event.data?.type === 'boyi-ronghui-layout-query') sync(true);
    if (event.data?.type === 'boyi-ronghui-layout-set' && typeof event.data.focused === 'boolean') {
      preferred = event.data.focused;
      if (preferred) {
        const tabs = window.mini?.get('mainTabs');
        const entry = tabs?.getTabs().find(tab => tab.title === '运单录入');
        // Only activate an already-open tab. Opening a new entry would allocate a waybill.
        if (entry && tabs.getActiveTab() !== entry) tabs.activeTab(entry);
      }
      sync(true);
    }
  });
  new MutationObserver(queue).observe(document, {
    subtree: true, childList: true, attributes: true, attributeFilter: ['class', 'style']
  });
  window.addEventListener('load', queue);
  sync();
})();
