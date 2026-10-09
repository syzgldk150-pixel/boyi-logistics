(() => {
  // The Console changes modules without a document navigation.
  if (location.origin !== 'https://boyi.homes') return;
  const pending = new WeakSet();
  const mounted = new WeakSet();
  async function prepare(frame) {
    if (pending.has(frame) || frame.dataset.entryFrameBound !== '1' || !frame.dataset.entryPendingSrc) return;
    let source;
    try { source = new URL(frame.dataset.entryPendingSrc); } catch { return; }
    const provider = source.origin === 'https://tms.ronghuiwl.com' && source.pathname === '/module/index' ? 'ronghui' :
      source.origin === 'https://v5.800best.com' && source.pathname === '/baseService/transOrder/networkProductOrder' ? 'best' : null;
    if (!provider) return;
    const statusKey = provider + 'Extension';
    pending.add(frame);
    frame.dataset[statusKey] = 'preparing';
    delete frame.dataset.originalPagePrepareError;
    try {
      const result = await chrome.runtime.sendMessage({type:'prepare-' + provider + '-embed'});
      if (!result?.ok) throw new Error('Preparation failed');
      if (!frame.isConnected) return;
      frame.dataset[statusKey] = '0.4.11';
      delete frame.dataset.entryPendingSrc;
      frame.dispatchEvent(new Event('console:original-page-reload'));
      if (provider === 'best') frame.sandbox.add('allow-storage-access-by-user-activation');
      frame.src = source.href;
    } catch (error) {
      frame.dataset[statusKey] = 'failed';
      // Reloading an unpacked extension invalidates scripts in existing pages.
      // Retrying only the provider iframe cannot reconnect that host document.
      if (String(error?.message).includes('Extension context invalidated')) {
        frame.dataset.originalPagePrepareError = 'extension-reloaded';
      }
      frame.dispatchEvent(new Event('console:original-page-prepare-failed'));
    } finally { pending.delete(frame); }
  }
  function scan() {
    document.querySelectorAll('iframe[data-ronghui-live-frame], iframe[data-best-live-frame]').forEach(frame => {
      if (mounted.has(frame)) return;
      mounted.add(frame);
      frame.addEventListener('console:original-page-prepare', () => prepare(frame));
      prepare(frame);
    });
  }
  new MutationObserver(records => {
    scan();
    records.filter(record => record.type === 'attributes' && mounted.has(record.target))
      .forEach(record => prepare(record.target));
  }).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['data-entry-pending-src']});
  scan();
})();
