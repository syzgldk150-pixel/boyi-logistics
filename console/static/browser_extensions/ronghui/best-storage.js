(() => {
  if (location.origin !== 'https://v5.800best.com' ||
      location.pathname !== '/baseService/transOrder/networkProductOrder' ||
      window.parent === window || location.ancestorOrigins?.[0] !== 'https://boyi.homes') return;
  // BEST keeps its native profile and site catalogue in localStorage/IndexedDB.
  // Use the browser's same-origin storage handle, without reading or copying data.
  // Its bundles bind localStorage/indexedDB when they evaluate, so a cached
  // bundle could otherwise bind the empty partitioned storage before the handle
  // arrives: the page then misses the sender site and loads no products.
  const held = [];
  let settled = false;
  const nativeBundle = node => node.tagName === 'SCRIPT' && node.src &&
    new URL(node.src).origin === location.origin && new URL(node.src).pathname.endsWith('.bundle.js');
  const observer = new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (settled || !nativeBundle(node)) return;
    // Runs before the parser prepares the script, so the bundle stays unexecuted.
    held.push([node, node.getAttribute('type')]);
    node.type = 'javascript/blocked';
  })));
  observer.observe(document, {childList:true, subtree:true});
  function release() {
    settled = true;
    observer.disconnect();
    held.forEach(([blocked, type]) => {
      const script = document.createElement('script');
      Array.from(blocked.attributes).forEach(({name, value}) => script.setAttribute(name, value));
      if (type === null) script.removeAttribute('type'); else script.setAttribute('type', type);
      script.async = false; // Keep the native bundle order.
      blocked.replaceWith(script);
    });
  }
  const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), 10000));
  (async () => {
    const handle = await Promise.race([
      document.requestStorageAccess({localStorage:true, indexedDB:true}), timeout
    ]);
    Object.defineProperty(window, 'localStorage', {configurable:true, get:() => handle.localStorage});
    Object.defineProperty(window, 'indexedDB', {configurable:true, get:() => handle.indexedDB});
  })().catch(() => {
    window.parent.postMessage({type:'boyi-best-storage-unavailable'}, 'https://boyi.homes');
  }).finally(release);
})();
