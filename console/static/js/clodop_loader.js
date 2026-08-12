(function (global) {
  "use strict";

  const DEFAULT_TIMEOUT_MS = 8000;
  const WEBSOCKET_URLS = [
    "ws://localhost:8000/CLodopfuncs.js",
    "ws://localhost:18000/CLodopfuncs.js",
  ];
  const HTTP_URLS = [
    "http://localhost:8000/CLodopfuncs.js",
    "http://localhost:18000/CLodopfuncs.js",
  ];
  const HTTPS_URLS = [
    "https://localhost.lodop.net:8443/CLodopfuncs.js",
  ];

  let loadPromise = null;
  let loadErrors = [];

  const describeError = (error) => {
    if (error instanceof Error) return error.message || error.name || "";
    return String(error || "");
  };

  const cleanUrl = (url) => String(url || "").split("?")[0];

  const getObject = () => {
    if (typeof global.getCLodop !== "function") return null;
    try {
      return global.getCLodop() || null;
    } catch {
      return null;
    }
  };

  const waitForObject = (timeoutMs = DEFAULT_TIMEOUT_MS) => new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const tick = () => {
      const lodop = getObject();
      if (lodop) {
        resolve(lodop);
        return;
      }
      if (Date.now() - startedAt >= timeoutMs) {
        reject(new Error("C-Lodop startup timeout"));
        return;
      }
      global.setTimeout(tick, 120);
    };
    tick();
  });

  const injectServiceScript = (url, source) => {
    const text = String(source || "").trim();
    if (!text) throw new Error("C-Lodop returned an empty service script");
    if (typeof global.getCLodop === "function") return;
    const script = document.createElement("script");
    script.dataset.clodopInlineSrc = url;
    script.text = `${text}\n//# sourceURL=${cleanUrl(url)}`;
    document.head.appendChild(script);
  };

  const loadViaWebSocket = (url, timeoutMs = 2500) => new Promise((resolve, reject) => {
    let settled = false;
    let socket;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      global.clearTimeout(timer);
      callback(value);
    };
    const timer = global.setTimeout(() => {
      try {
        socket?.close();
      } catch {
        // The socket may already have been closed by C-Lodop.
      }
      finish(reject, new Error("C-Lodop WebSocket load timeout"));
    }, timeoutMs);
    try {
      socket = new global.WebSocket(url);
      socket.onmessage = (event) => {
        try {
          injectServiceScript(url, event.data);
          finish(resolve);
        } catch (error) {
          finish(reject, error);
        }
      };
      socket.onerror = () => finish(reject, new Error("C-Lodop WebSocket unavailable"));
      socket.onclose = () => {
        if (!settled && typeof global.getCLodop !== "function") {
          finish(reject, new Error("C-Lodop WebSocket closed before loading"));
        }
      };
    } catch (error) {
      finish(reject, error);
    }
  });

  const loadViaScript = (url, timeoutMs = DEFAULT_TIMEOUT_MS) => new Promise((resolve, reject) => {
    const existing = document.querySelector(`script[data-clodop-src="${url}"]`);
    if (existing) {
      resolve();
      return;
    }
    const script = document.createElement("script");
    const timer = global.setTimeout(() => {
      script.remove();
      reject(new Error("C-Lodop script load timeout"));
    }, timeoutMs);
    script.src = url;
    script.crossOrigin = "anonymous";
    script.referrerPolicy = "no-referrer";
    script.dataset.clodopSrc = url;
    script.onload = () => {
      global.clearTimeout(timer);
      resolve();
    };
    script.onerror = () => {
      global.clearTimeout(timer);
      script.remove();
      reject(new Error("C-Lodop script unavailable"));
    };
    document.head.appendChild(script);
  });

  const recordLoadError = (url, error) => {
    const detail = describeError(error) || "加载失败";
    loadErrors.push(`${cleanUrl(url)}：${detail}`);
  };

  const load = async () => {
    const existing = getObject();
    if (existing) return existing;

    loadErrors = [];
    if (typeof global.WebSocket === "function") {
      for (const url of WEBSOCKET_URLS) {
        try {
          await loadViaWebSocket(url);
          return await waitForObject();
        } catch (error) {
          recordLoadError(url, error);
        }
      }
    }

    const scriptUrls = global.location.protocol === "https:" ? HTTPS_URLS : HTTP_URLS;
    for (const url of scriptUrls) {
      try {
        await loadViaScript(url);
        return await waitForObject();
      } catch (error) {
        recordLoadError(url, error);
      }
    }

    throw new Error("C-Lodop service not found");
  };

  const getInstance = () => {
    const existing = getObject();
    if (existing) return Promise.resolve(existing);
    if (!loadPromise) {
      loadPromise = load().catch((error) => {
        loadPromise = null;
        throw error;
      });
    }
    return loadPromise;
  };

  global.BoyiCLodop = Object.freeze({
    getInstance,
    getLoadErrors: () => loadErrors.slice(),
    peek: getObject,
  });
})(window);
