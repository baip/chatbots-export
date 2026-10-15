/*
 * Export chats on genai.umass.edu (LibreChat) as JSON, using the site's own
 * Share > Export dialog with every option checked (Include endpoint options,
 * Export all message branches, Recursive). Convert to Markdown afterwards with
 * librechat_json_to_md.py.
 *
 * Why version 1 produced empty files: the script exported about 2.5 s after opening each
 * chat. The site builds the export from the messages it has already loaded, so when a
 * chat had not finished loading, the file had "messages": [] (under 1 KB).
 * This version
 *   1. waits until the chat's messages are on screen and have stopped changing,
 *   2. reads each export before it is saved, blocks the download if it has no messages,
 *      and retries with longer waits (up to 5 tries),
 *   3. downloads JSON only.
 *
 * How to use:
 *   1. Open any chat on the site, open DevTools (F12) > Console, paste this file, press Enter.
 *   2. If Chrome asks "This site is trying to download multiple files", click Allow.
 *   3. To stop early:  window.__stopExport = true
 *   4. Re-running in the same tab skips chats already exported (window.__done).
 *
 * To export only some chats, put their conversation IDs (the part after /c/ in the URL)
 * in ONLY_IDS. Leave it empty to export every chat in the sidebar.
 */
(async () => {
  const ONLY_IDS = [
    // 'conversation-id-1', 'conversation-id-2',
  ];
  const CFG = {
    loadTimeout: 45000,          // ms to wait for a chat's messages to appear
    retryWaits: [3000, 6000, 10000, 15000, 20000], // extra waits before each retry of an empty export
    navIdleRounds: 5,            // stop scrolling the sidebar after N rounds with nothing new
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log('%c[export]', 'color:#a33;font-weight:bold', ...a);
  const vis = (e) => !!e && e.getClientRects().length > 0;
  const txt = (e) => (e?.innerText || e?.textContent || '').trim();
  async function waitFor(fn, t = 6000) {
    const t0 = Date.now();
    while (Date.now() - t0 < t) { const v = fn(); if (v) return v; await sleep(150); }
    return null;
  }
  function pclick(el) {
    const o = { bubbles: true, cancelable: true, view: window, button: 0 };
    el.dispatchEvent(new PointerEvent('pointerdown', { ...o, pointerType: 'mouse' }));
    el.dispatchEvent(new MouseEvent('mousedown', o));
    el.dispatchEvent(new PointerEvent('pointerup', { ...o, pointerType: 'mouse' }));
    el.dispatchEvent(new MouseEvent('mouseup', o));
    el.dispatchEvent(new MouseEvent('click', o));
  }
  async function clickUntil(el, check, t = 2000) {
    el.click(); if (await waitFor(check, t)) return true;
    pclick(el); return !!(await waitFor(check, t));
  }

  // ---------- 0. Download gate: inspect each JSON export before Chrome saves it ----------
  const gate = (window.__exportGate = window.__exportGate || { last: null, blocked: 0 });
  if (!window.__exportGateInstalled) {
    window.__exportGateInstalled = true;
    const OrigBlob = window.Blob;
    const PatchedBlob = function (parts, opts) {
      const b = new OrigBlob(parts, opts);
      try { if (parts && parts.length === 1 && typeof parts[0] === 'string') b.__text = parts[0]; } catch (e) {}
      return b;
    };
    PatchedBlob.prototype = OrigBlob.prototype;
    window.Blob = PatchedBlob;
    const urlBlobs = new Map();
    const origCreate = URL.createObjectURL;
    URL.createObjectURL = function (obj) { const u = origCreate.call(URL, obj); urlBlobs.set(u, obj); return u; };
    const payload = (a) => {
      const h = a.href || '';
      if (h.startsWith('blob:')) return urlBlobs.get(h)?.__text ?? null;
      if (h.startsWith('data:')) {
        const i = h.indexOf(','), meta = h.slice(5, i), body = h.slice(i + 1);
        try { return meta.includes('base64') ? decodeURIComponent(escape(atob(body))) : decodeURIComponent(body); }
        catch (e) { return null; }
      }
      return null;
    };
    const allow = (a) => {
      if (!a.download || !/\.json$/i.test(a.download)) return true;
      const t = payload(a);
      let n = null;
      if (t != null) { try { n = (JSON.parse(t).messages || []).length; } catch (e) {} }
      gate.last = { name: a.download, messages: n, at: Date.now() };
      if (n === 0) { gate.blocked++; return false; } // empty export: do not save it
      return true;
    };
    const origClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () { if (allow(this)) return origClick.apply(this, arguments); };
    const origDispatch = HTMLAnchorElement.prototype.dispatchEvent;
    HTMLAnchorElement.prototype.dispatchEvent = function (ev) {
      if (ev && ev.type === 'click' && !allow(this)) return false;
      return origDispatch.apply(this, arguments);
    };
  }

  // ---------- 1. Scroll the sidebar to the very bottom, collecting every chat ----------
  const g = document.querySelector('[role="grid"][aria-label="Conversations"]');
  if (!g) { log('Sidebar chat list not found. Open the sidebar and try again.'); return; }
  const convoOf = (el) => {
    const fk = Object.keys(el).find((k) => k.startsWith('__reactFiber'));
    let f = el[fk];
    for (let i = 0; i < 8 && f; i++, f = f.return) {
      const p = f.memoizedProps || {}; const cv = p.conversation || p.convo;
      if (cv && cv.conversationId) return cv;
    }
    return null;
  };
  const chats = new Map();
  const collect = () => g.querySelectorAll('[data-testid="convo-item"]').forEach((el) => {
    const cv = convoOf(el); if (cv && !chats.has(cv.conversationId)) chats.set(cv.conversationId, cv.title);
  });
  let idle = 0, lastH = -1, lastN = -1;
  while (idle < CFG.navIdleRounds) {
    // sweep from the very top each round: the sidebar list is virtualized, and the page
    // may start out scrolled part way down
    for (let y = 0; y <= g.scrollHeight; y += 200) { g.scrollTop = y; await sleep(60); collect(); }
    await sleep(1500); collect();
    if (g.scrollHeight === lastH && chats.size === lastN) idle++;
    else { idle = 0; lastH = g.scrollHeight; lastN = chats.size; }
  }
  const targets = [...chats].filter(([id]) => !ONLY_IDS.length || ONLY_IDS.includes(id));
  const notFound = ONLY_IDS.filter((id) => !chats.has(id));
  log(`Found ${chats.size} chats in the sidebar. Exporting ${targets.length}.`);
  if (notFound.length) log('These IDs are not in the sidebar:', notFound);

  // ---------- 2. Per-chat steps ----------
  async function openChat(id) {
    const find = () => [...g.querySelectorAll('[data-testid="convo-item"]')].find((e) => convoOf(e)?.conversationId === id);
    let el = find();
    for (let y = 0; !el && y <= g.scrollHeight; y += 150) { g.scrollTop = y; await sleep(80); el = find(); }
    if (!el) throw new Error('chat not found in sidebar');
    el.scrollIntoView({ block: 'center' }); await sleep(200); el.click();
    if (!(await waitFor(() => location.pathname.endsWith(id), 10000))) throw new Error('navigation failed');
  }
  // Messages are "loaded" when message elements exist and the page text stops changing.
  function messageSignal() {
    const main = document.querySelector('main') || document.body;
    const nav = document.querySelectorAll('[aria-label^="Go to user message"], [aria-label^="Go to assistant message"]').length;
    const msgs = main.querySelectorAll('.message-render, [data-testid*="message" i]').length;
    return { count: Math.max(nav, msgs), len: txt(main).length };
  }
  async function waitForMessages() {
    const t0 = Date.now();
    let prev = null, stable = 0;
    while (Date.now() - t0 < CFG.loadTimeout) {
      const s = messageSignal();
      if (s.count > 0 && prev && s.count === prev.count && s.len === prev.len) { if (++stable >= 3) return true; }
      else stable = 0;
      prev = s; await sleep(700);
    }
    return false;
  }
  async function scrollChatToTop() {
    const main = document.querySelector('main') || document.body;
    const pick = () => [...main.querySelectorAll('*')]
      .filter((e) => e.scrollHeight > e.clientHeight + 50 && /auto|scroll/.test(getComputedStyle(e).overflowY))
      .sort((a, b) => b.scrollHeight - a.scrollHeight)[0];
    // short chats may not need a scroll container, so wait for one only briefly
    let sc = await waitFor(pick, 5000); if (!sc) return;
    // scroll to the top repeatedly until the height and message count stop growing
    let h = -1, c = -1, still = 0;
    while (still < 3) {
      sc.scrollTop = 0; await sleep(1200); sc = pick() || sc;
      const n = messageSignal().count;
      if (sc.scrollHeight === h && n === c && sc.scrollTop === 0) still++;
      else { still = 0; h = sc.scrollHeight; c = n; }
    }
  }
  const dialog = () => [...document.querySelectorAll('[role="dialog"]')].filter(vis).pop();
  async function openDialog() {
    const btn = document.querySelector('button[aria-label="Export options"]');
    if (!btn) throw new Error('"Export options" (share) button missing');
    const item = () => [...document.querySelectorAll('[role="menuitem"]')].find((e) => vis(e) && /^export$/i.test(txt(e)));
    if (!(await clickUntil(btn, item))) throw new Error('share menu did not open');
    if (!(await clickUntil(item(), dialog, 3000))) throw new Error('export dialog did not open');
    await sleep(400); return dialog();
  }
  async function setJson(d) {
    const cb = d.querySelector('[role="combobox"]');
    const re = /^json/i;
    if (!re.test(txt(cb))) {
      const opt = () => [...document.querySelectorAll('[role="option"]')].find((o) => vis(o) && re.test(txt(o)));
      if (!(await clickUntil(cb, opt))) throw new Error('type list did not open');
      await clickUntil(opt(), () => re.test(txt(cb)));
      if (!re.test(txt(cb))) throw new Error('could not select JSON');
    }
    await sleep(400);
    // check every option: Include endpoint options, Export all message branches, Recursive
    for (const box of (dialog() || d).querySelectorAll('[role="checkbox"]')) {
      if (box.disabled || box.getAttribute('aria-disabled') === 'true') continue;
      const on = () => box.getAttribute('aria-checked') === 'true';
      if (!on()) await clickUntil(box, on, 1500);
    }
    if ((dialog() || d).querySelector('#includeOptions')?.getAttribute('aria-checked') !== 'true')
      throw new Error('could not check "Include endpoint options"');
  }
  function closeDialog() {
    const d = dialog();
    if (d) [...d.querySelectorAll('button')].find((b) => /^cancel$/i.test(txt(b)))?.click();
  }
  async function exportChat(id) {
    await openChat(id);
    if (!(await waitForMessages())) log('  messages did not appear on screen; trying the export anyway');
    await scrollChatToTop();
    for (let attempt = 0; attempt <= CFG.retryWaits.length; attempt++) {
      const d = dialog() || (await openDialog());
      await setJson(d);
      gate.last = null;
      const t0 = Date.now();
      [...d.querySelectorAll('button')].find((b) => /^export$/i.test(txt(b))).click();
      await waitFor(() => gate.last && gate.last.at >= t0, 5000);
      closeDialog(); await sleep(400);
      const r = gate.last;
      if (r && r.messages > 0) return `${r.messages} top-level message(s)`;
      if (r && r.messages === null) return 'saved (contents could not be checked)';
      if (!r) throw new Error('no download was triggered');
      if (attempt < CFG.retryWaits.length) {
        log(`  export was empty, waiting ${CFG.retryWaits[attempt] / 1000}s and retrying`);
        await sleep(CFG.retryWaits[attempt]);
        await scrollChatToTop();
      }
    }
    throw new Error('export stayed empty after all retries (nothing was saved)');
  }

  // ---------- 3. Run ----------
  window.__done = window.__done || new Set();
  const failures = {};
  let n = 0;
  for (const [id, title] of targets) {
    n++;
    if (window.__stopExport) { log('Stopped.'); break; }
    if (window.__done.has(id)) continue;
    log(`${n}/${targets.length}: ${title}`);
    try { const r = await exportChat(id); window.__done.add(id); log(`  ok: ${r}`); }
    catch (e) {
      failures[title] = e.message; log(`  FAILED: ${e.message}`);
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await sleep(800);
    }
  }
  window.__exportFailures = failures;
  log(`Done. ${Object.keys(failures).length} failure(s). Empty exports blocked: ${gate.blocked}.`, failures);
})();
