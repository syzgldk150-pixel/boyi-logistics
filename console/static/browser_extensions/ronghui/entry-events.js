(() => {
  const RONGHUI = 'https://tms.ronghuiwl.com';
  const ancestors = location.ancestorOrigins;
  if (location.origin !== RONGHUI || location.pathname !== '/widget/home' ||
      ancestors?.length !== 2 || ancestors[0] !== RONGHUI ||
      ancestors[1] !== 'https://boyi.homes') return;

  function install() {
    const address = window.mini?.get('ACCEPT_MAN_ADDRESS');
    if (!address || address.__boyiAddressEvents || typeof address.fire !== 'function' ||
        typeof address.getValue !== 'function') return;
    const fire = address.fire;
    // Native blur/enter handlers call test(e.value), but MiniUI omits value on
    // those events. Supply the current field before the original handlers run.
    address.fire = function (type, event, ...args) {
      if (type === 'blur' || type === 'enter') {
        event = event || {};
        if (event.value == null) event.value = this.getValue();
      }
      return fire.call(this, type, event, ...args);
    };
    address.__boyiAddressEvents = true;
  }
  install();
  window.addEventListener('load', install, {once: true});
})();
