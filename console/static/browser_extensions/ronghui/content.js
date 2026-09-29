(() => {
  if (location.pathname !== '/ocr') return;
  const pending = new WeakSet();
  const prepared = new WeakSet();
  async function mount(frame) {
    if (pending.has(frame) || prepared.has(frame)) return;
    let source;
    try { source = new URL(frame.src); } catch { return; }
    if (source.hostname !== 'tms.ronghuiwl.com') return;
    pending.add(frame);
    try {
      const result = await chrome.runtime.sendMessage({type:'prepare-ronghui-embed'});
      if (!result?.ok || !frame.isConnected) { frame.dataset.ronghuiExtension='failed'; return; }
      prepared.add(frame);
      frame.dataset.ronghuiExtension = '0.3.0';
      frame.src = 'https://tms.ronghuiwl.com/module/index?mv=index';
    } finally { pending.delete(frame); }
  }
  function scan() { document.querySelectorAll('iframe[data-ronghui-live-frame]').forEach(mount); }
  new MutationObserver(scan).observe(document,{subtree:true,childList:true,attributes:true,attributeFilter:['src']});
  scan();
})();
