(() => {
  // The Console changes modules without a document navigation.
  if (location.origin !== 'https://boyi.homes') return;
  const pending = new WeakSet();
  const mounted = new WeakSet();
  async function prepare(frame) {
    if (pending.has(frame) || frame.dataset.entryFrameBound !== '1' || !frame.dataset.entryPendingSrc) return;
    let source;
    try { source = new URL(frame.dataset.entryPendingSrc); } catch { return; }
    if (source.origin !== 'https://tms.ronghuiwl.com' || source.pathname !== '/module/index') return;
    pending.add(frame);
    frame.dataset.ronghuiExtension = 'preparing';
    try {
      const result = await chrome.runtime.sendMessage({type:'prepare-ronghui-embed'});
      if (!result?.ok) throw new Error('Preparation failed');
      if (!frame.isConnected) return;
      frame.dataset.ronghuiExtension = '0.4.2';
      delete frame.dataset.entryPendingSrc;
      frame.dispatchEvent(new Event('console:original-page-reload'));
      frame.src = source.href;
    } catch {
      frame.dataset.ronghuiExtension = 'failed';
      frame.dispatchEvent(new Event('console:original-page-prepare-failed'));
    } finally { pending.delete(frame); }
  }
  function scan() {
    document.querySelectorAll('iframe[data-ronghui-live-frame]').forEach(frame => {
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
