(() => {
  // The native createOrder URL opens Drafts. Select its actual form tab once.
  if (location.origin !== 'https://v5.800best.com' ||
      location.pathname !== '/baseService/transOrder/createOrder' ||
      window.parent === window || location.ancestorOrigins?.[0] !== 'https://boyi.homes') return;
  let attempted = false, complete = false;
  function check() {
    if (complete || location.pathname !== '/baseService/transOrder/createOrder') return;
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
