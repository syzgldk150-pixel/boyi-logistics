(() => {
  const root = document.querySelector('[data-monitoring-dashboard]');
  if (!root) return;
  const pageRoot = root.closest('.main-content');
  const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
  const find = (name) => root.querySelector(`[data-monitoring-${name}]`);
  const els = {
    total: find('total'), yunda: find('yunda'), ronghui: find('ronghui'), exception: find('exception'),
    updated: find('updated'),
    systems: find('systems'), detailTitle: find('detail-title'), detailSubtitle: find('detail-subtitle'),
    items: find('items'), itemsStatus: find('items-status'), pagination: find('pagination'),
    pageLabel: find('page-label'), prev: find('prev'), next: find('next'), open: find('open'), reload: find('reload'),
    refresh: document.querySelector('[data-monitoring-refresh]'), agentStatus: find('agent-status')
  };
  const monitoringRequestTimeoutMs = 8000;
  const monitoringItemsTimeoutMs = 75000;
  const systemNodes = new Map();
  let source = null;
  let summaryTimer = 0;
  let refreshTimer = 0;
  let refreshCooldown = false;
  let latestSnapshotKey = '';
  let latestItemsSnapshotKey = '';
  let latestSnapshot = null;
  let activeCategory = null;
  let activeOpenUrl = '';
  let currentPage = 1;
  let displayedItemsKey = '';
  let displayedItemsUpdatedAt = '';
  let itemsRequest = null;
  let itemsRequestSeq = 0;
  let summaryRequest = null;
  let disposed = false;

  function pageHidden() {
    return disposed || document.hidden || Boolean(pageRoot?.hidden);
  }

  function setText(el, text) {
    if (el && el.textContent !== String(text)) el.textContent = String(text);
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) setText(node, text);
    return node;
  }

  function fmt(value) {
    return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value.toLocaleString('zh-CN') : '—';
  }

  function categoryKey(item) {
    return `${item?.system || ''}:${item?.category_id || ''}`;
  }

  function safeOpenUrl(value) {
    if (!value) return '';
    try {
      const url = new URL(value);
      const official = ['ronghuiwl.com', 'yunda56.com'].some((host) => url.hostname === host || url.hostname.endsWith(`.${host}`));
      return url.protocol === 'https:' && official && !url.username && !url.password && !url.port ? url.href : '';
    } catch (_) {
      return '';
    }
  }

  function setOpenUrl(value) {
    activeOpenUrl = safeOpenUrl(value);
    els.open.disabled = !activeOpenUrl;
  }

  function setItemsStatus(text, status = '', stale = false) {
    setText(els.itemsStatus, text);
    els.itemsStatus.dataset.status = status;
    els.itemsStatus.dataset.stale = String(stale);
  }

  function setItemsBusy(busy) {
    els.items.setAttribute('aria-busy', String(busy));
    els.reload.disabled = busy || !activeCategory?.items_supported;
    els.prev.disabled = busy || currentPage <= 1;
    els.next.disabled = busy || els.next.dataset.hasMore !== 'true';
  }

  function setRefreshing(busy) {
    if (!els.refresh) return;
    els.refresh.disabled = busy;
    els.refresh.classList.toggle('is-loading', busy);
    els.refresh.setAttribute('aria-busy', String(busy));
  }

  async function fetchMonitoringJson(url, controller = new AbortController(), allowFailure = false, timeoutMs = monitoringRequestTimeoutMs) {
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await pageFetch(url, { credentials: 'same-origin', signal: controller.signal });
      const payload = await response.json();
      if (!response.ok || (!allowFailure && payload?.ok === false)) throw new Error(payload?.message || '智能服务不可用');
      return payload;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function renderSummaryUnavailable() {
    if (disposed) return;
    latestSnapshotKey = '';
    [els.total, els.yunda, els.ronghui, els.exception].forEach((node) => setText(node, '—'));
    const lastUpdate = latestSnapshot?.updated_at ? `，上次同步 ${latestSnapshot.updated_at}` : '';
    setText(els.updated, `同步失败${lastUpdate}`);
    els.agentStatus.hidden = false;
    if (!latestSnapshot) {
      els.systems.replaceChildren(element('p', 'monitor-empty', '智能服务不可用，消息来源暂时无法获取。'));
      setItemsStatus('消息暂时无法获取，请稍后刷新。', 'error');
    } else {
      systemNodes.forEach((panel) => {
        const note = panel.querySelector('[data-monitoring-source-note]');
        setText(note, `同步失败，以下为上次取得的分类${lastUpdate}`);
        note.hidden = false;
        note.dataset.status = 'error';
      });
      const hasItems = els.items.childElementCount > 0;
      const previousUpdate = hasItems ? `上次更新：${displayedItemsUpdatedAt || '时间未提供'}。` : '';
      setItemsStatus(`同步失败，当前内容尚未更新。${previousUpdate}请稍后刷新。`, 'error', hasItems);
    }
  }

  function getSystemPanel(systemId) {
    let panel = systemNodes.get(systemId);
    if (panel) return panel;
    panel = element('article', 'monitor-panel');
    panel.dataset.monitoringSystem = systemId;
    const header = element('div', 'monitor-panel-header');
    const title = element('div', 'monitor-panel-title');
    const label = element('h2');
    label.dataset.monitoringSystemLabel = '';
    const status = element('span', 'monitor-status');
    status.dataset.monitoringSystemStatus = '';
    title.append(label);
    header.append(title, status);
    const note = element('p', 'monitor-source-note');
    note.dataset.monitoringSourceNote = '';
    const list = element('div', 'monitor-category-list');
    list.dataset.monitoringCategoryList = '';
    panel.append(header, note, list);
    systemNodes.set(systemId, panel);
    return panel;
  }

  function createCategoryButton(item) {
    const button = element('button', 'monitor-category');
    button.type = 'button';
    button.dataset.monitoringCategory = '';
    button.dataset.system = item.system;
    button.dataset.categoryId = item.category_id;
    button.dataset.tone = item.tone || 'info';
    button.dataset.count = typeof item.count === 'number' ? String(item.count) : 'unknown';
    const active = categoryKey(item) === categoryKey(activeCategory);
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
    const icon = element('span', 'monitor-category-icon');
    const symbol = element('i');
    symbol.setAttribute('data-feather', item.is_exception ? 'alert-triangle' : 'inbox');
    icon.append(symbol);
    const text = element('span');
    text.append(element('span', 'monitor-category-title', item.title || '消息分类'));
    const status = [item.status_label, item.message].filter(Boolean).join(' · ');
    const meta = element('span', 'monitor-category-meta', status || (item.items_supported ? '查看消息正文' : '暂未提供正文'));
    meta.title = meta.textContent;
    text.append(meta);
    button.append(icon, text, element('span', 'monitor-category-count', fmt(item.count)));
    return button;
  }

  function allCategories(snapshot) {
    return (Array.isArray(snapshot?.systems) ? snapshot.systems : []).flatMap((system) =>
      (Array.isArray(system.categories) ? system.categories : []).map((item) => ({
        ...item, system: system.system, source_label: system.label || system.system,
        source_status: system.status, source_message: system.message, source_updated_at: system.updated_at,
        stale: Boolean(item.stale || system.stale || snapshot.stale)
      }))
    );
  }

  function renderSnapshot(snapshot) {
    if (pageHidden()) return;
    if (snapshot?.ok === false) { renderSummaryUnavailable(); return; }
    const key = JSON.stringify(snapshot);
    if (key === latestSnapshotKey) return;
    latestSnapshotKey = key;
    latestSnapshot = snapshot;
    els.agentStatus.hidden = true;
    const totals = snapshot.totals || {};
    setText(els.total, fmt(totals.total_pending));
    setText(els.yunda, fmt(totals.yunda_pending));
    setText(els.ronghui, fmt(totals.ronghui_pending));
    setText(els.exception, fmt(totals.exception_pending));
    const updated = snapshot.updated_at ? `最后同步 ${snapshot.updated_at}` : '等待同步';
    setText(els.updated, `${updated}${snapshot.stale ? ' · 缓存已过期' : ''}${snapshot.refreshing ? ' · 后台刷新中' : ''}`);
    const categories = allCategories(snapshot);
    const previousKey = categoryKey(activeCategory);
    activeCategory = categories.find((item) => categoryKey(item) === previousKey)
      || categories.find((item) => item.items_supported && item.count > 0)
      || categories.find((item) => item.items_supported) || null;
    const panels = [];
    const seen = new Set();
    (Array.isArray(snapshot.systems) ? snapshot.systems : []).forEach((system) => {
      if (!system.system) return;
      seen.add(system.system);
      const panel = getSystemPanel(system.system);
      setText(panel.querySelector('[data-monitoring-system-label]'), system.label || system.system);
      const status = panel.querySelector('[data-monitoring-system-status]');
      setText(status, system.status_label || '状态未确认');
      status.dataset.status = system.status || '';
      const note = panel.querySelector('[data-monitoring-source-note]');
      const noteParts = [system.message];
      if (system.stale || snapshot.stale) noteParts.push('缓存已过期');
      if (system.updated_at) noteParts.push(`更新于 ${system.updated_at}`);
      setText(note, noteParts.filter(Boolean).join(' · '));
      note.dataset.status = system.status || '';
      note.hidden = !note.textContent;
      const list = panel.querySelector('[data-monitoring-category-list]');
      const systemCategories = categories.filter((item) => item.system === system.system);
      const contentCategories = systemCategories.filter((item) => item.items_supported);
      const otherCategories = systemCategories.filter((item) => !item.items_supported);
      const keepOthersOpen = Boolean(list.querySelector('.monitor-other-categories')?.open);
      list.replaceChildren(...contentCategories.map(createCategoryButton));
      if (otherCategories.length) {
        const other = element('details', 'monitor-other-categories');
        other.open = keepOthersOpen || otherCategories.some((item) => categoryKey(item) === categoryKey(activeCategory));
        const summary = element('summary', '', `其他原系统待办（${otherCategories.length}类）`);
        const otherList = element('div', 'monitor-category-list');
        otherList.append(...otherCategories.map(createCategoryButton));
        other.append(summary, otherList);
        list.append(other);
      }
      if (!systemCategories.length) list.append(element('p', 'monitor-empty', system.message || '当前没有可读取的消息分类。'));
      panels.push(panel);
    });
    systemNodes.forEach((_, id) => { if (!seen.has(id)) systemNodes.delete(id); });
    els.systems.replaceChildren(...panels);
    if (!panels.length) els.systems.append(element('p', 'monitor-empty', '当前没有可读取的消息来源。'));
    if (window.feather) window.feather.replace();
    if (!activeCategory) {
      clearItems();
      setText(els.detailTitle, '消息与收到工单');
      setText(els.detailSubtitle, '暂无可读取正文的分类');
      setItemsStatus('请查看来源状态，或选择分类了解原因。');
      return;
    }
    if (categoryKey(activeCategory) !== previousKey) {
      currentPage = 1;
      clearItems();
    }
    const itemsSnapshotKey = JSON.stringify([snapshot.updated_at, activeCategory, currentPage]);
    if (itemsSnapshotKey !== latestItemsSnapshotKey) {
      latestItemsSnapshotKey = itemsSnapshotKey;
      fetchItems();
    }
  }

  function clearItems() {
    itemsRequest?.controller.abort();
    itemsRequest = null;
    itemsRequestSeq += 1;
    displayedItemsKey = '';
    displayedItemsUpdatedAt = '';
    els.items.replaceChildren();
    els.pagination.hidden = true;
    els.next.dataset.hasMore = 'false';
    setOpenUrl('');
    setItemsBusy(false);
  }

  function createMessageItem(item, expanded) {
    const row = element('li', 'monitor-item');
    const details = element('details');
    details.dataset.itemId = String(item.id || '');
    details.open = expanded.has(String(item.id || ''));
    const summary = element('summary', 'monitor-item-summary');
    summary.append(element('span', 'monitor-item-title', item.title || '消息'));
    const meta = element('span', 'monitor-item-meta');
    [item.source_label || activeCategory.source_label, item.occurred_at || '时间未提供', item.status_label || '状态未确认',
      item.waybill_no ? `运单 ${item.waybill_no}` : ''].filter(Boolean).forEach((text) => meta.append(element('span', '', text)));
    const content = item.content || '原系统未提供消息正文。';
    summary.append(meta, element('span', 'monitor-item-preview', content), element('span', 'monitor-item-hint', '展开查看完整正文'));
    details.append(summary, element('p', 'monitor-item-content', content));
    row.append(details);
    const openUrl = safeOpenUrl(item.open_url);
    if (openUrl) {
      const link = element('a', 'monitor-item-link', '去原系统处理');
      link.href = openUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      row.append(link);
    }
    return row;
  }

  async function fetchItems(force = false) {
    if (!activeCategory || pageHidden()) return;
    const selected = activeCategory;
    const key = `${categoryKey(selected)}:${currentPage}`;
    if (itemsRequest?.key === key && !force) return;
    itemsRequest?.controller.abort();
    const seq = ++itemsRequestSeq;
    setText(els.detailTitle, selected.title || '消息与收到工单');
    setText(els.detailSubtitle, `${selected.source_label} · ${selected.status_label || '状态未确认'}`);
    setOpenUrl(selected.open_url);
    if (!selected.items_supported) {
      clearItems();
      setOpenUrl(selected.open_url);
      setItemsStatus(selected.message || selected.source_message || '此分类暂未提供消息正文，请打开原系统查看。');
      return;
    }
    const preserve = displayedItemsKey === key;
    if (!preserve) {
      els.items.replaceChildren();
      els.pagination.hidden = true;
    }
    setItemsBusy(true);
    setItemsStatus(preserve ? '正在更新消息…' : '正在读取消息正文…');
    const controller = new AbortController();
    itemsRequest = { key, controller };
    const params = new URLSearchParams({ system: selected.system, category_id: selected.category_id, page: String(currentPage) });
    if (force) params.set('force', '1');
    try {
      const payload = await fetchMonitoringJson(`/monitoring/items?${params}`, controller, true, monitoringItemsTimeoutMs);
      if (seq !== itemsRequestSeq) return;
      setText(els.detailSubtitle, `${selected.source_label} · ${payload.status_label || selected.status_label || '状态未确认'}`);
      if (payload.ok === false) throw new Error(payload.message || '该来源的消息读取失败');
      if (!Array.isArray(payload.items)) throw new Error('消息响应不完整，请稍后刷新');
      setOpenUrl(payload.open_url || selected.open_url);
      const expanded = new Set(Array.from(els.items.querySelectorAll('details[open]')).map((node) => node.dataset.itemId));
      els.items.replaceChildren(...payload.items.map((item) => createMessageItem(item, expanded)));
      displayedItemsKey = key;
      displayedItemsUpdatedAt = String(payload.updated_at || '');
      const stale = Boolean(payload.stale || selected.stale);
      const updated = payload.updated_at ? `更新于 ${payload.updated_at}` : '更新时间未提供';
      const note = payload.items.length ? (payload.message || '') : (payload.message || '当前分类没有待处理消息或新回复。');
      setItemsStatus([note, updated, stale ? '缓存已过期，等待同步' : ''].filter(Boolean).join(' · '), payload.status || '', stale);
      els.pagination.hidden = !payload.items.length && currentPage === 1;
      setText(els.pageLabel, `第 ${currentPage} 页${typeof payload.total === 'number' ? ` · 共 ${fmt(payload.total)} 条` : ' · 总量待确认'}`);
      els.next.dataset.hasMore = String(payload.has_more === true);
    } catch (error) {
      if (seq !== itemsRequestSeq || pageHidden()) return;
      const message = error?.name === 'AbortError' ? '消息读取超时，请刷新重试' : (error.message || '消息读取失败');
      const previousUpdate = preserve ? `上次更新：${displayedItemsUpdatedAt || '时间未提供'}。` : '';
      setItemsStatus(`${message}${preserve ? '；以下为上次取得的内容，尚未更新。' : '。'}${previousUpdate}`, 'error', preserve);
      els.next.dataset.hasMore = 'false';
    } finally {
      if (seq === itemsRequestSeq) {
        itemsRequest = null;
        setItemsBusy(false);
      }
    }
  }

  function preferCachedQueryValue(preferCached) {
    return preferCached ? 'prefer_cached=1' : 'prefer_cached=0';
  }

  async function fetchSummary(force = false, preferCached = false) {
    if (summaryRequest) return summaryRequest;
    summaryRequest = fetchMonitoringJson(`/monitoring/summary?systems=yunda,ronghui&force=${force ? '1' : '0'}&${preferCachedQueryValue(preferCached)}`)
      .then(renderSnapshot).catch(renderSummaryUnavailable).finally(() => { summaryRequest = null; });
    return summaryRequest;
  }

  function startUpdates() {
    if (pageHidden()) return;
    fetchSummary(false, true);
    if (window.EventSource && !source) {
      source = new EventSource('/monitoring/stream');
      source.addEventListener('snapshot', (event) => {
        try { renderSnapshot(JSON.parse(event.data || '{}')); } catch (_) { renderSummaryUnavailable(); }
      });
      source.onerror = () => fetchSummary(false, true);
    }
    window.clearInterval(summaryTimer);
    summaryTimer = window.setInterval(() => fetchSummary(false, true), 60000);
  }

  function stopUpdates() {
    source?.close();
    source = null;
    window.clearInterval(summaryTimer);
    window.clearTimeout(refreshTimer);
    itemsRequest?.controller.abort();
    itemsRequest = null;
    itemsRequestSeq += 1;
    setItemsBusy(false);
    refreshCooldown = false;
    setRefreshing(false);
  }

  els.open.addEventListener('click', () => {
    if (activeOpenUrl) window.open(activeOpenUrl, '_blank', 'noopener,noreferrer');
  });
  els.reload.addEventListener('click', () => fetchItems(true));
  els.prev.addEventListener('click', () => { if (currentPage > 1) { currentPage -= 1; fetchItems(); } });
  els.next.addEventListener('click', () => { if (els.next.dataset.hasMore === 'true') { currentPage += 1; fetchItems(); } });
  els.systems.addEventListener('click', (event) => {
    const button = event.target.closest('[data-monitoring-category]');
    if (!button || !els.systems.contains(button)) return;
    const item = allCategories(latestSnapshot).find((candidate) => candidate.system === button.dataset.system && candidate.category_id === button.dataset.categoryId);
    if (!item) return;
    if (categoryKey(item) !== categoryKey(activeCategory)) { clearItems(); currentPage = 1; }
    activeCategory = item;
    els.systems.querySelectorAll('[data-monitoring-category]').forEach((node) => {
      const active = node === button;
      node.classList.toggle('is-active', active);
      node.setAttribute('aria-pressed', String(active));
    });
    fetchItems();
  });
  els.refresh?.addEventListener('click', () => {
    if (refreshCooldown) return;
    refreshCooldown = true;
    setRefreshing(true);
    Promise.allSettled([fetchSummary(true, true), fetchItems(true)]).finally(() => {
      if (pageHidden()) return;
      refreshTimer = window.setTimeout(() => {
        fetchSummary(false, false);
        refreshCooldown = false;
        setRefreshing(false);
      }, 1200);
    });
  });
  function onVisibilityChange() {
    if (pageHidden()) { stopUpdates(); return; }
    latestItemsSnapshotKey = '';
    latestSnapshotKey = '';
    startUpdates();
  }
  document.addEventListener('visibilitychange', onVisibilityChange);
  const visibilityObserver = new MutationObserver(onVisibilityChange);
  if (pageRoot) visibilityObserver.observe(pageRoot, { attributes: true, attributeFilter: ['hidden'] });
  window.addEventListener('pagehide', stopUpdates);
  function onPageShow(event) {
    if (event.persisted) {
      latestItemsSnapshotKey = '';
      latestSnapshotKey = '';
      startUpdates();
    }
  }
  window.addEventListener('pageshow', onPageShow);
  window.ConsoleUI?.onPageCleanup(() => {
    disposed = true;
    stopUpdates();
    visibilityObserver.disconnect();
    document.removeEventListener('visibilitychange', onVisibilityChange);
    window.removeEventListener('pagehide', stopUpdates);
    window.removeEventListener('pageshow', onPageShow);
  });
  startUpdates();
})();
