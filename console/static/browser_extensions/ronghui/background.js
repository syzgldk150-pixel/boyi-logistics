const HOST = 'tms.ronghuiwl.com';
const activeTabs = new Set();
const ready = chrome.declarativeNetRequest.getSessionRules().then(rules => {
  rules.forEach(rule => (rule.condition.tabIds || []).forEach(id => activeTabs.add(id)));
});
const stats = {adapted:0, failed:0, captured:0};
globalThis.embedProofStats = stats;
function applicableDomain(domain) {
  const value = domain.replace(/^\./, '').toLowerCase();
  return HOST === value || HOST.endsWith('.' + value);
}
function existingCookieDetails(cookie) {
  const details = {url:'https://' + HOST + cookie.path, name:cookie.name,
    value:cookie.value, path:cookie.path, secure:true, sameSite:'no_restriction',
    httpOnly:cookie.httpOnly, storeId:cookie.storeId};
  if (!cookie.hostOnly) details.domain = cookie.domain;
  if (!cookie.session) details.expirationDate = cookie.expirationDate;
  if (cookie.partitionKey) details.partitionKey = cookie.partitionKey;
  return details;
}
function receivedCookieDetails(header, responseUrl) {
  const u = new URL(responseUrl);
  if (u.hostname !== HOST || u.protocol !== 'https:') return null;
  const parts = header.split(';');
  const pair = parts.shift();
  const equals = pair.indexOf('=');
  if (equals <= 0) return null;
  const details = {url:u.origin + u.pathname, name:pair.slice(0,equals).trim(),
    value:pair.slice(equals+1).trim(), secure:true, sameSite:'no_restriction',httpOnly:false};
  let maxAge;
  for (const piece of parts) {
    const split = piece.indexOf('=');
    const key = (split < 0 ? piece : piece.slice(0,split)).trim().toLowerCase();
    const value = split < 0 ? '' : piece.slice(split+1).trim();
    if (key === 'domain') { if (!applicableDomain(value)) return null; details.domain = value; }
    if (key === 'path' && value.startsWith('/')) details.path = value;
    if (key === 'httponly') details.httpOnly = true;
    if (key === 'expires' && Number.isFinite(Date.parse(value))) details.expirationDate = Date.parse(value)/1000;
    if (key === 'max-age' && /^-?\d+$/.test(value)) maxAge = Number(value);
    if (key === 'partitioned') return null;
  }
  if (maxAge !== undefined) details.expirationDate = maxAge <= 0 ? 1 : Date.now()/1000 + maxAge;
  return details;
}
async function adaptCookie(details) {
  try { await chrome.cookies.set(details); stats.adapted++; }
  catch { stats.failed++; }
}
chrome.webRequest.onHeadersReceived.addListener(async response => {
  await ready;
  if (!activeTabs.has(response.tabId)) return;
  const headers = (response.responseHeaders || []).filter(h => h.name.toLowerCase() === 'set-cookie');
  for (const header of headers) {
    if (typeof header.value !== 'string') continue;
    const details = receivedCookieDetails(header.value,response.url);
    if (details) { stats.captured++; await adaptCookie(details); }
  }
}, {urls:['https://' + HOST + '/*']}, ['responseHeaders','extraHeaders']);
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (message?.type !== 'prepare-ronghui-embed') return;
  const origin = new URL(sender.url || 'https://invalid.local');
  if (origin.origin !== 'https://boyi.homes' || sender.frameId !== 0 || !sender.tab) {
    reply({ok:false}); return;
  }
  (async () => {
    await ready;
    const tabId = sender.tab.id;
    activeTabs.add(tabId);
    const currentRules = await chrome.declarativeNetRequest.getSessionRules();
    const tabIds = [...activeTabs];
    await chrome.declarativeNetRequest.updateSessionRules({
      removeRuleIds:currentRules.map(rule => rule.id),
      addRules:[
        {id:1,priority:1,action:{type:'upgradeScheme'},
          condition:{urlFilter:'|http://' + HOST + '/',tabIds}},
        {id:2,priority:2,action:{type:'modifyHeaders',responseHeaders:[
          {header:'location',operation:'set',value:'https://' + HOST + '/system/login'}]},
          condition:{requestDomains:[HOST],tabIds,responseHeaders:[
            {header:'location',values:['http://' + HOST + '/system/login*']}]} }
      ]
    });
    const cookies = await chrome.cookies.getAll({domain:HOST});
    await Promise.all(cookies.filter(c => applicableDomain(c.domain)).map(c => adaptCookie(existingCookieDetails(c))));
    reply({ok:stats.failed===0});
  })().catch(() => reply({ok:false}));
  return true;
});
chrome.tabs.onRemoved.addListener(async tabId => {
  await ready;
  activeTabs.delete(tabId);
  const rules = await chrome.declarativeNetRequest.getSessionRules();
  const tabIds = [...activeTabs];
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds:rules.map(rule => rule.id),
    addRules:tabIds.length ? rules.map(rule => ({...rule,condition:{...rule.condition,tabIds}})) : []
  });
});
