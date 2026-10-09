(() => {
  // MiniUI writes login data such as userInfo through document.cookie without
  // SameSite. Chrome treats that as Lax and silently drops it in the cross-site
  // Boyi embed, so the entry init later reads no profile. Standalone tabs stay native.
  const ancestors = location.ancestorOrigins;
  if (location.origin !== 'https://tms.ronghuiwl.com' || window.top === window ||
      ancestors?.[ancestors.length - 1] !== 'https://boyi.homes') return;
  const cookie = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
  Object.defineProperty(Document.prototype, 'cookie', {...cookie, set(value) {
    // Keep the native name, value, path, expiry and domain; replace only the
    // attributes that decide whether this cross-site frame may store it.
    const [pair, ...attributes] = String(value).split(';');
    const kept = attributes.filter(item => !/^\s*(samesite|secure)\s*(=|$)/i.test(item));
    cookie.set.call(this, [pair, ...kept, ' SameSite=None', ' Secure'].join(';'));
  }});
})();
