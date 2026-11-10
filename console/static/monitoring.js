(() => {
  const root = document.querySelector('[data-monitoring-dashboard]');
  if (!root) return;
  const pageRoot = root.closest('.main-content');
  const pageFetch = window.ConsoleUI?.pageRequest ? window.ConsoleUI.pageRequest() : window.fetch.bind(window);
  const find = (name) => root.querySelector(`[data-monitoring-${name}]`);
  const els = {
    metrics: Array.from(root.querySelectorAll('[data-monitoring-metric]')),
    updated: find('updated'),
    systems: find('systems'), detailTitle: find('detail-title'), detailSubtitle: find('detail-subtitle'),
    items: find('items'), itemsStatus: find('items-status'), pagination: find('pagination'),
    pageLabel: find('page-label'), prev: find('prev'), next: find('next'), open: find('open'), reload: find('reload'),
    refresh: document.querySelector('[data-monitoring-refresh]'), agentStatus: find('agent-status'),
    syncFlags: find('sync-flags'), sourceAlert: find('source-alert'), sourceSwitch: find('source-switch'),
    pendingMore: root.querySelector('[data-overview-pending-more]'),
    pendingMetric: root.querySelector('[data-overview-pending-metric]')
  };
  // Each card reads its own categories; only disjoint scopes are summed.
  const metricDefs = {
    'ronghui-work': { sum: ['ronghui:work_response', 'ronghui:work_processing'], tone: 'warning',
      note: [['ronghui:work_response', '待响应'], ['ronghui:work_processing', '待处理']] },
    'yunda-work': { sum: ['yunda:work_orders'], tone: 'warning', note: [['yunda:work_order_replies', '催单待回复']] },
    announcements: { sum: ['yunda:announcements', 'ronghui:announcements'],
      note: [['yunda:announcements', '韵达'], ['ronghui:announcements', '融辉']] },
    'yunda-notifications': { sum: ['yunda:notifications'], note: [] }
  };
  const monitoringRequestTimeoutMs = 8000;
  const monitoringItemsTimeoutMs = 75000;
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
  // Message-center tabs: one source filter plus the customer-service “待确认” tab.
  let activeSource = 'all';
  let pendingState = { available: false, total: null };
  let pendingActive = false;

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
    els.metrics.forEach((node) => setText(node.querySelector('[data-monitoring-metric-value]'), '—'));
    const lastUpdate = latestSnapshot?.updated_at ? `，上次同步 ${latestSnapshot.updated_at}` : '';
    setText(els.updated, `同步失败${lastUpdate}`);
    if (els.updated) els.updated.dataset.status = 'error';
    els.agentStatus.hidden = false;
    if (!latestSnapshot) {
      els.systems.replaceChildren(element('p', 'monitor-empty', '智能服务不可用，消息来源暂时无法获取。'));
      setItemsStatus('消息暂时无法获取，请稍后刷新。', 'error');
    } else {
      setSourceAlert([`同步失败，以下为上次取得的分类${lastUpdate}`], 'error');
      const hasItems = els.items.childElementCount > 0;
      const previousUpdate = hasItems ? `上次更新：${displayedItemsUpdatedAt || '时间未提供'}。` : '';
      setItemsStatus(`同步失败，当前内容尚未更新。${previousUpdate}请稍后刷新。`, 'error', hasItems);
    }
  }

  function setSourceAlert(lines, status = '') {
    if (!els.sourceAlert) return;
    const text = lines.filter(Boolean).join('；');
    setText(els.sourceAlert, text);
    els.sourceAlert.dataset.status = status;
    els.sourceAlert.hidden = !text;
  }

  function sourceTag(label, system) {
    const tag = element('span', 'monitor-source-tag', label || system || '来源');
    tag.dataset.system = system || '';
    return tag;
  }

  function createCategoryButton(item) {
    const button = element('button', 'monitor-category');
    button.type = 'button';
    button.setAttribute('role', 'tab');
    button.dataset.monitoringCategory = '';
    button.dataset.system = item.system;
    button.dataset.categoryId = item.category_id;
    button.dataset.tone = item.tone || 'info';
    button.dataset.count = typeof item.count === 'number' ? String(item.count) : 'unknown';
    const active = !pendingActive && categoryKey(item) === categoryKey(activeCategory);
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', String(active));
    if (activeSource === 'all') button.append(element('span', 'monitor-category-source', shortSource(item.source_label, item.system)));
    button.append(element('span', 'monitor-category-title', item.title || '消息分类'));
    button.append(element('span', 'monitor-category-count', fmt(item.count)));
    const failed = item.status === 'error' || item.status === 'auth_required';
    const status = [item.status_label, item.message].filter(Boolean).join(' · ');
    button.title = (failed ? status : item.status_label) || (item.items_supported ? '查看消息正文' : '暂未提供正文，请打开原系统查看');
    return button;
  }

  function createPendingTab() {
    const button = element('button', 'monitor-category monitor-category--pending');
    button.type = 'button';
    button.setAttribute('role', 'tab');
    button.dataset.monitoringPendingTab = '';
    button.classList.toggle('is-active', pendingActive);
    button.setAttribute('aria-selected', String(pendingActive));
    button.append(element('span', 'monitor-category-title', '待确认'), element('span', 'monitor-category-count', fmt(pendingState.total)));
    button.title = '客户服务中等待回复的问题件';
    return button;
  }

  function shortSource(label, system) {
    if (system === 'yunda') return '韵达';
    if (system === 'ronghui') return '融辉';
    return String(label || system || '').split(' ')[0];
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

  function renderMetrics(categories) {
    const byId = new Map(categories.map((item) => [item.category_id, item]));
    els.metrics.forEach((node) => {
      const def = metricDefs[node.dataset.monitoringMetric];
      if (!def) return;
      const counts = def.sum.map((id) => byId.get(id)?.count);
      const known = counts.every((value) => typeof value === 'number');
      const total = known ? counts.reduce((sum, value) => sum + value, 0) : null;
      setText(node.querySelector('[data-monitoring-metric-value]'), fmt(total));
      node.dataset.tone = def.tone && total > 0 ? def.tone : 'neutral';
      if (def.note.length) {
        setText(node.querySelector('[data-monitoring-metric-note]'),
          def.note.map(([id, label]) => `${label} ${fmt(byId.get(id)?.count)}`).join(' · '));
      }
      const active = !pendingActive && def.sum.includes(activeCategory?.category_id);
      node.classList.toggle('is-active', active);
      node.setAttribute('aria-pressed', String(active));
    });
    els.pendingMetric?.classList.toggle('is-active', pendingActive);
  }

  function selectCategory(item) {
    if (!item) return;
    if (pendingActive || categoryKey(item) !== categoryKey(activeCategory)) { clearItems(); currentPage = 1; }
    pendingActive = false;
    if (els.pendingMore) els.pendingMore.hidden = true;
    activeCategory = item;
    els.systems.querySelectorAll('[data-monitoring-category]').forEach((node) => {
      const active = node.dataset.system === item.system && node.dataset.categoryId === item.category_id;
      node.classList.toggle('is-active', active);
      node.setAttribute('aria-selected', String(active));
    });
    els.systems.querySelector('[data-monitoring-pending-tab]')?.classList.remove('is-active');
    renderMetrics(allCategories(latestSnapshot));
    fetchItems();
  }

  function renderSnapshot(snapshot) {
    if (pageHidden()) return;
    if (snapshot?.ok === false) { renderSummaryUnavailable(); return; }
    const key = JSON.stringify(snapshot);
    if (key === latestSnapshotKey) return;
    latestSnapshotKey = key;
    latestSnapshot = snapshot;
    els.agentStatus.hidden = true;
    const updated = snapshot.updated_at ? `最后同步 ${snapshot.updated_at}` : '等待同步';
    setText(els.updated, updated);
    if (els.updated) els.updated.dataset.status = snapshot.stale ? 'stale' : 'ok';
    const categories = allCategories(snapshot);
    const previousKey = categoryKey(activeCategory);
    const sourceCategories = categories.filter((item) => activeSource === 'all' || item.system === activeSource);
    activeCategory = pendingActive ? null : sourceCategories.find((item) => categoryKey(item) === previousKey)
      || sourceCategories.find((item) => item.items_supported && item.is_exception && item.count > 0)
      || sourceCategories.find((item) => item.items_supported && item.count > 0)
      || sourceCategories.find((item) => item.items_supported) || null;
    renderTabs(snapshot, categories);
    const alerts = (Array.isArray(snapshot.systems) ? snapshot.systems : [])
      .filter((system) => system.status === 'error' || system.status === 'auth_required' || system.stale || (system.message && system.status !== 'ok'))
      .map((system) => [system.label || system.system, system.message || system.status_label, system.stale ? '缓存已过期' : '']
        .filter(Boolean).join(' · '));
    setSourceAlert(alerts, alerts.length ? 'warning' : '');
    setSyncFlags(snapshot);
    renderMetrics(categories);
    if (window.feather) window.feather.replace();
    if (pendingActive) return;
    if (!activeCategory) {
      clearItems();
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

  function setSyncFlags(snapshot) {
    if (!els.syncFlags) return;
    const flags = [snapshot?.stale ? '缓存已过期' : '', snapshot?.refreshing ? '后台刷新中' : ''].filter(Boolean);
    setText(els.syncFlags, flags.join(' · '));
    els.syncFlags.hidden = !flags.length;
  }

  function renderTabs(snapshot, categories) {
    const visible = categories.filter((item) => activeSource === 'all' || item.system === activeSource);
    // Categories with readable bodies first, exceptions before notices; idle categories collapse into a note.
    const content = visible.filter((item) => item.items_supported)
      .sort((a, b) => Number(Boolean(b.is_exception)) - Number(Boolean(a.is_exception)));
    const other = visible.filter((item) => !item.items_supported && item.count !== 0);
    const idle = visible.length - content.length - other.length;
    const tabs = [];
    if (pendingState.available) tabs.push(createPendingTab());
    tabs.push(...content.map(createCategoryButton), ...other.map(createCategoryButton));
    if (idle > 0) tabs.push(element('span', 'monitor-other-empty', `其余 ${idle} 类无待办`));
    if (!tabs.length) tabs.push(element('p', 'monitor-empty', snapshot?.systems?.length ? '当前来源没有可读取的消息分类。' : '当前没有可读取的消息来源。'));
    els.systems.replaceChildren(...tabs);
    els.sourceSwitch?.querySelectorAll('[data-source]').forEach((button) => {
      const active = button.dataset.source === activeSource;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function selectPending() {
    if (!pendingState.available) return;
    clearItems();
    pendingActive = true;
    activeCategory = null;
    renderTabs(latestSnapshot, allCategories(latestSnapshot));
    renderMetrics(allCategories(latestSnapshot));
    setText(els.detailSubtitle, '客户服务 · 等待回复的问题件，按等待时间排序');
    setItemsStatus('正在读取待确认问题件…');
    if (els.pendingMore) els.pendingMore.hidden = false;
    root.dispatchEvent(new CustomEvent('overview:pending-render', { detail: { list: els.items, status: els.itemsStatus } }));
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
    const title = item.title || '消息';
    const head = element('div', 'monitor-item-head');
    head.append(sourceTag(shortSource(activeCategory.source_label, activeCategory.system), activeCategory.system),
      element('span', 'monitor-item-kind', activeCategory.title || '消息'),
      element('span', 'monitor-item-title', title));
    const aside = element('div', 'monitor-item-aside');
    aside.append(element('span', 'monitor-item-status is-status', item.status_label || '状态未确认'),
      element('span', 'monitor-item-time', item.occurred_at || '时间未提供'));
    const openUrl = safeOpenUrl(item.open_url);
    if (openUrl) {
      const link = element('a', 'monitor-item-link');
      link.href = openUrl;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.title = '去原系统处理';
      link.setAttribute('aria-label', `去原系统处理：${title}`);
      const icon = element('i');
      icon.setAttribute('data-feather', 'arrow-up-right');
      link.append(icon);
      aside.append(link);
    }
    head.append(aside);
    const meta = element('div', 'monitor-item-meta');
    // Ronghui labels repeat the selected category; Yunda labels name the push channel.
    const source = item.source_label && !item.source_label.endsWith(activeCategory.title) ? item.source_label : '';
    [item.waybill_no ? `运单 ${item.waybill_no}` : '', source]
      .filter(Boolean).forEach((text) => meta.append(element('span', '', text)));
    row.append(head);
    const content = String(item.content || '').trim();
    if (!content) {
      meta.append(element('span', 'monitor-item-content is-empty', '原系统未提供消息正文。'));
      row.append(meta);
    } else if (content === title) {
      if (meta.childElementCount) row.append(meta);
    } else if (content.length <= 80 && !content.includes('\n')) {
      // Short bodies are shown in full; long ones collapse to one line.
      meta.append(element('span', 'monitor-item-content', content));
      row.append(meta);
    } else {
      if (meta.childElementCount) row.append(meta);
      const details = element('details');
      details.dataset.itemId = String(item.id || '');
      details.open = expanded.has(String(item.id || ''));
      const summary = element('summary', 'monitor-item-summary');
      summary.append(element('span', 'monitor-item-preview', content), element('span', 'monitor-item-hint', '展开查看完整正文'));
      details.append(summary, element('p', 'monitor-item-content', content));
      row.append(details);
    }
    return row;
  }

  async function fetchItems(force = false) {
    if (pendingActive || !activeCategory || pageHidden()) return;
    const selected = activeCategory;
    const key = `${categoryKey(selected)}:${currentPage}`;
    if (itemsRequest?.key === key && !force) return;
    itemsRequest?.controller.abort();
    const seq = ++itemsRequestSeq;
    setText(els.detailSubtitle, `${selected.source_label} · ${selected.title || '消息分类'} · ${selected.status_label || '状态未确认'}`);
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
      setText(els.detailSubtitle, `${selected.source_label} · ${selected.title || '消息分类'} · ${payload.status_label || selected.status_label || '状态未确认'}`);
      if (payload.ok === false) throw new Error(payload.message || '该来源的消息读取失败');
      if (!Array.isArray(payload.items)) throw new Error('消息响应不完整，请稍后刷新');
      setOpenUrl(payload.open_url || selected.open_url);
      const expanded = new Set(Array.from(els.items.querySelectorAll('details[open]')).map((node) => node.dataset.itemId));
      els.items.replaceChildren(...payload.items.map((item) => createMessageItem(item, expanded)));
      if (window.feather) window.feather.replace();
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
  els.reload.addEventListener('click', () => {
    if (pendingActive) { root.dispatchEvent(new CustomEvent('overview:pending-reload')); return; }
    fetchItems(true);
  });
  els.prev.addEventListener('click', () => { if (currentPage > 1) { currentPage -= 1; fetchItems(); } });
  els.next.addEventListener('click', () => { if (els.next.dataset.hasMore === 'true') { currentPage += 1; fetchItems(); } });
  els.systems.addEventListener('click', (event) => {
    if (event.target.closest('[data-monitoring-pending-tab]')) { selectPending(); return; }
    const button = event.target.closest('[data-monitoring-category]');
    if (!button || !els.systems.contains(button)) return;
    selectCategory(allCategories(latestSnapshot).find((candidate) => candidate.system === button.dataset.system && candidate.category_id === button.dataset.categoryId));
  });
  els.sourceSwitch?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-source]');
    if (!button || button.dataset.source === activeSource) return;
    activeSource = button.dataset.source;
    if (pendingActive) { renderTabs(latestSnapshot, allCategories(latestSnapshot)); return; }
    latestSnapshotKey = '';
    if (latestSnapshot) renderSnapshot(latestSnapshot); else renderTabs(null, []);
  });
  els.pendingMetric?.addEventListener('click', selectPending);
  function onPendingState(event) {
    const detail = event.detail || {};
    pendingState = { available: Boolean(detail.available), total: typeof detail.total === 'number' ? detail.total : null };
    if (!pendingState.available && pendingActive) {
      pendingActive = false;
      if (els.pendingMore) els.pendingMore.hidden = true;
      latestSnapshotKey = '';
      if (latestSnapshot) renderSnapshot(latestSnapshot);
      return;
    }
    renderTabs(latestSnapshot, allCategories(latestSnapshot));
    if (pendingActive) root.dispatchEvent(new CustomEvent('overview:pending-render', { detail: { list: els.items, status: els.itemsStatus } }));
  }
  root.addEventListener('overview:pending', onPendingState);
  els.metrics.forEach((node) => node.addEventListener('click', () => {
    const ids = metricDefs[node.dataset.monitoringMetric]?.sum || [];
    const candidates = allCategories(latestSnapshot).filter((item) => ids.includes(item.category_id) && item.items_supported);
    selectCategory(candidates.find((item) => item.count > 0) || candidates[0]);
  }));
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
    root.removeEventListener('overview:pending', onPendingState);
    document.removeEventListener('visibilitychange', onVisibilityChange);
    window.removeEventListener('pagehide', stopUpdates);
    window.removeEventListener('pageshow', onPageShow);
  });
  startUpdates();
})();
