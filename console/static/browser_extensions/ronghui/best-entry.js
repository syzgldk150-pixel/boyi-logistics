(() => {
  // Select the native entry form tab once, leaving subsequent choices untouched.
  if (location.origin !== 'https://v5.800best.com' ||
      location.pathname !== '/baseService/transOrder/networkProductOrder' ||
      window.parent === window || location.ancestorOrigins?.[0] !== 'https://boyi.homes') return;
  let attempted = false, complete = false;
  function check() {
    if (complete || location.pathname !== '/baseService/transOrder/networkProductOrder') return;
    const tabs = Array.from(document.querySelectorAll('[role="tab"]'))
      .filter(tab => tab.textContent.trim() === '运单录入');
    if (tabs.length !== 1) return;
    if (!attempted) {
      attempted = true;
      if (tabs[0].getAttribute('aria-selected') !== 'true') tabs[0].click();
    }
    if (document.querySelector('[role="tabpanel"][aria-hidden="false"] #sendPhone') &&
        document.querySelector('[role="tabpanel"][aria-hidden="false"] #code')) {
      complete = true;
      observer.disconnect();
      clearTimeout(deadline);
    }
  }
  const observer = new MutationObserver(check);
  observer.observe(document, {subtree:true, childList:true});
  const deadline = setTimeout(() => {
    observer.disconnect();
    if (!complete) window.parent.postMessage({type:'boyi-best-entry-unavailable'}, 'https://boyi.homes');
  }, 30000);
  window.navigation?.addEventListener('navigatesuccess', check);
  check();
})();

(() => {
  const BOYI = 'https://boyi.homes';
  const ENTRY = '/baseService/transOrder/networkProductOrder';
  if (location.origin !== 'https://v5.800best.com' || location.pathname !== ENTRY ||
      window.parent === window || location.ancestorOrigins?.[0] !== BOYI) return;
  const root = document.documentElement;
  const focusedClass = 'boyi-best-entry-focus';
  const layoutClass = 'boyi-best-entry-layout';
  const measuringClass = 'boyi-best-layout-measuring';
  const style = document.createElement('style');
  style.id = 'boyi-best-entry-layout';
  style.textContent = `
    html.${focusedClass} .g-page > .g-header { display: none !important; }
    html.${focusedClass} .g-page > .g-main {
      padding-top: calc(var(--boyi-best-main-padding) - var(--boyi-best-header-height)) !important;
    }
    html.${focusedClass} .g-page > .g-main > .m-header-menu {
      top: calc(var(--boyi-best-tabs-top) - var(--boyi-best-header-height)) !important;
    }
    html.${layoutClass} .p-networkCreateOrder .order-btns {
      position: static !important; top: auto !important;
      left: auto !important; right: auto !important; width: auto !important;
    }
    html.${focusedClass} .p-networkCreateOrder .s-table-toolbar {
      top: calc(var(--boyi-best-toolbar-top) - var(--boyi-best-header-height)) !important;
    }
    html.${measuringClass} .g-page > .g-main,
    html.${measuringClass} .g-page > .g-main > .m-header-menu,
    html.${measuringClass} .p-networkCreateOrder .s-table-toolbar {
      transition: none !important;
    }
  `;
  (document.head || root).append(style);
  let preferred = true, scheduled = false, remeasure = false, previousState = '';
  let measuredHeader, measuredMain, measuredTabs;
  let measuredToolbars = [];
  function sync(force = false) {
    const header = document.querySelector('.g-page > .g-header');
    const main = document.querySelector('.g-page > .g-main');
    const tabs = main?.querySelector(':scope > .m-header-menu');
    const toolbars = Array.from(document.querySelectorAll(
      '.p-networkCreateOrder .s-table-toolbar'
    ));
    const toolbarsChanged = toolbars.length !== measuredToolbars.length ||
      toolbars.some((toolbar, index) => toolbar !== measuredToolbars[index]);
    let available = location.pathname === ENTRY && Boolean(header && main && tabs);
    if (available && (remeasure || header !== measuredHeader || main !== measuredMain ||
        tabs !== measuredTabs || toolbarsChanged)) {
      // Measure the native layout, including responsive changes. Keep all DOM
      // nodes and drafts in place; removing our class restores native styling.
      const wasFocused = root.classList.contains(focusedClass);
      // The native tabs animate top. Disable transitions through both layout
      // changes so measurements use native target values, never an in-flight top.
      root.classList.add(measuringClass);
      root.classList.remove(focusedClass);
      const height = header.getBoundingClientRect().height;
      const padding = parseFloat(getComputedStyle(main).paddingTop);
      const top = parseFloat(getComputedStyle(tabs).top);
      available = height > 0 && Number.isFinite(padding) && Number.isFinite(top);
      if (available) {
        root.style.setProperty('--boyi-best-header-height', height + 'px');
        root.style.setProperty('--boyi-best-main-padding', padding + 'px');
        root.style.setProperty('--boyi-best-tabs-top', top + 'px');
        toolbars.forEach(toolbar => {
          const nativeStyle = getComputedStyle(toolbar);
          const nativeTop = parseFloat(nativeStyle.top);
          if (nativeStyle.position === 'fixed' && Number.isFinite(nativeTop)) {
            toolbar.style.setProperty('--boyi-best-toolbar-top', nativeTop + 'px');
          }
        });
        measuredHeader = header; measuredMain = main; measuredTabs = tabs;
        measuredToolbars = toolbars;
      }
      root.classList.toggle(focusedClass, wasFocused);
      // Flush the restored style before enabling native transitions again.
      getComputedStyle(tabs).top;
      root.classList.remove(measuringClass);
    }
    remeasure = false;
    const focused = available && preferred;
    const layoutChanged = root.classList.contains(layoutClass) !== available;
    const focusChanged = root.classList.contains(focusedClass) !== focused;
    if (layoutChanged) root.classList.toggle(layoutClass, available);
    if (focusChanged) {
      root.classList.toggle(focusedClass, focused);
    }
    if (layoutChanged || focusChanged) {
      window.dispatchEvent(new Event('resize'));
    }
    const state = JSON.stringify({available, focused});
    if (force || state !== previousState) {
      previousState = state;
      window.parent.postMessage({type:'boyi-best-layout-state', available, focused}, BOYI);
    }
  }
  function queue(measure = false) {
    remeasure = remeasure || measure;
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; sync(); });
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.origin !== BOYI) return;
    if (event.data?.type === 'boyi-best-layout-query') sync(true);
    if (event.data?.type === 'boyi-best-layout-set' && typeof event.data.focused === 'boolean') {
      preferred = event.data.focused;
      sync(true);
    }
  });
  new MutationObserver(records => {
    // Our root layout classes must not retry an unmeasurable hidden iframe.
    if (records.some(record => record.type === 'childList' || record.target !== root)) queue();
  }).observe(document, {
    subtree:true, childList:true, attributes:true, attributeFilter:['class', 'style']
  });
  window.addEventListener('resize', () => queue(true));
  window.addEventListener('load', () => queue(true));
  window.addEventListener('popstate', () => queue(true));
  window.navigation?.addEventListener('navigatesuccess', () => queue(true));
  sync();
})();
