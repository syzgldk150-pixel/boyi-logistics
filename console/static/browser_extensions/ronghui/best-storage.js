(() => {
  if (location.origin !== 'https://v5.800best.com' ||
      location.pathname !== '/baseService/transOrder/networkProductOrder' ||
      window.parent === window || location.ancestorOrigins?.[0] !== 'https://boyi.homes') return;
  // BEST keeps its native profile and site catalogue in localStorage/IndexedDB.
  // Use the browser's same-origin storage handle, without reading or copying data.
  (async () => {
    const handle = await document.requestStorageAccess({localStorage:true, indexedDB:true});
    Object.defineProperty(window, 'localStorage', {configurable:true, get:() => handle.localStorage});
    Object.defineProperty(window, 'indexedDB', {configurable:true, get:() => handle.indexedDB});
  })().catch(() => {
    window.parent.postMessage({type:'boyi-best-storage-unavailable'}, 'https://boyi.homes');
  });
})();
