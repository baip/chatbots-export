/*
 * Download a claude.ai data export from its manifest-*.json file.
 *
 * Why version 1 saved 139 KB HTML files instead of zips: an export link such as
 * https://claude.ai/export/<org>/download/<nonce> is a page in the claude.ai web app,
 * not the file. When you open it in a tab, the page sends a POST request to
 * /api/organizations/<org>/export_signed_url/<nonce>, gets back a short-lived signed
 * download_url, and only then downloads the zip from that address. Version 1 saved the
 * page itself under the zip's name.
 * This version
 *   1. makes the same POST request with your logged-in session to get the signed URL,
 *   2. downloads the file from the signed URL and checks that a .zip really is a zip
 *      before saving it under the manifest's file name,
 *   3. if the browser blocks reading the signed URL, falls back to a plain browser
 *      download and prints the URL so you can open it yourself.
 *
 * How to use:
 *   1. Open any page on https://claude.ai while logged in to the account that requested
 *      the export, open DevTools (F12 or Cmd+Option+J) > Console, paste this file, press Enter.
 *   2. Click "Choose export manifest..." in the panel at the top right and pick the
 *      manifest-*.json file from the export email.
 *   3. If Chrome asks "This site is trying to download multiple files", click Allow.
 *
 * Each export link works once and expires 24 hours after the export was created. Asking
 * for the signed URL is what uses the link up, so a link you already opened by hand
 * fails with "Invalid link" and the script moves on to the next one.
 *
 * To download only some files, put their file names (for example "conversations-000.zip")
 * in ONLY_FILES. Leave it empty to download every file in the manifest.
 */
(() => {
  const ONLY_FILES = [
    // 'conversations-000.zip', 'projects-000.zip',
  ];
  const CFG = {
    delayMs: 1500,             // pause between files
    linkTtlMs: 24 * 3600e3,    // manifest links expire 24 h after created_at
  };

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const log = (...a) => console.log('%c[claude-export]', 'color:#a33;font-weight:bold', ...a);
  const warn = (...a) => console.warn('[claude-export]', ...a);

  if (location.hostname !== 'claude.ai') {
    console.error('[claude-export] Run this on a logged-in https://claude.ai tab.');
    return;
  }

  // Map an export page link to the API call the page itself makes (same routes as the web app).
  function signedUrlEndpoint(exportUrl) {
    const u = new URL(exportUrl);
    if (u.origin !== location.origin) return null;
    const seg = String.raw`([A-Za-z0-9_-]+)`;
    let m;
    if ((m = u.pathname.match(new RegExp(`^/export/account/download/${seg}/?$`))))
      return `/api/account/export_signed_url/${m[1]}`;
    if ((m = u.pathname.match(new RegExp(`^/export/audit-logs/${seg}/download/${seg}/?$`))))
      return `/api/organizations/${m[1]}/audit_logs/export_signed_url/${m[2]}`;
    if ((m = u.pathname.match(new RegExp(`^/export/${seg}/download/${seg}/?$`))))
      return `/api/organizations/${m[1]}/export_signed_url/${m[2]}`;
    return null;
  }

  async function getSignedUrl(endpoint) {
    const res = await fetch(endpoint, { method: 'POST', credentials: 'include' });
    let body = {};
    try { body = await res.json(); } catch { /* non-JSON error page */ }
    if (!res.ok) {
      const code = body?.error?.details?.error_code || body?.error?.message || `HTTP ${res.status}`;
      const hint = {
        account_session_invalid: 'You are not logged in (or your session expired). Log in and run again.',
        incorrect_user: 'Log in with the account that requested the export.',
        too_many_downloads: 'Too many downloads in progress. Wait a moment and run again with ONLY_FILES.',
      }[code];
      throw new Error(hint ? `${code}: ${hint}` : `${code} (the link may be used up or expired)`);
    }
    const url = body.download_url || body.signed_url;
    if (!url) throw new Error('The response had no download_url.');
    return url;
  }

  function saveBlob(blob, filename) {
    const href = URL.createObjectURL(blob);
    const a = Object.assign(document.createElement('a'), { href, download: filename, style: 'display:none' });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 60e3);
  }

  // Fallback when the storage server does not allow the page to read the file:
  // let the browser download it directly in a hidden frame (the file name then comes from the server).
  function browserDownload(url) {
    const f = Object.assign(document.createElement('iframe'), { src: url, style: 'display:none' });
    document.body.append(f);
    setTimeout(() => f.remove(), 5 * 60e3);
  }

  async function downloadOne(item) {
    const endpoint = signedUrlEndpoint(item.export_url);
    if (!endpoint) throw new Error(`Not a claude.ai export link: ${item.export_url}`);
    const url = await getSignedUrl(endpoint);

    let res;
    try {
      res = await fetch(url, { credentials: 'omit' });
    } catch (e) {
      browserDownload(url);
      warn(`Could not read ${item.filename} directly (${e.message}). Started a browser download instead.`,
        'If no file appears, open this URL in a new tab before it expires:', url);
      return 'browser download started (not verified)';
    }
    if (!res.ok) throw new Error(`Download failed: HTTP ${res.status}`);
    const blob = await res.blob();

    const name = item.filename || new URL(url).pathname.split('/').pop() || 'export.zip';
    if (/\.zip$/i.test(name)) {
      const magic = new Uint8Array(await blob.slice(0, 2).arrayBuffer());
      if (!(magic[0] === 0x50 && magic[1] === 0x4b)) {
        console.log((await blob.slice(0, 300).text()));
        throw new Error(`Got ${blob.size} bytes that are not a zip, so nothing was saved (start of the response is above).`);
      }
    }
    saveBlob(blob, name);
    return `saved ${name} (${(blob.size / 1024).toFixed(1)} KB)`;
  }

  // ---- panel ----
  document.getElementById('__claude_export_dl')?.remove();
  const box = document.createElement('div');
  box.id = '__claude_export_dl';
  Object.assign(box.style, {
    position: 'fixed', top: '16px', right: '16px', zIndex: 2147483647,
    background: '#fff', color: '#111', border: '1px solid #888', borderRadius: '8px',
    padding: '10px 12px', font: '13px system-ui, sans-serif', maxWidth: '380px',
    boxShadow: '0 2px 10px rgba(0,0,0,.25)', whiteSpace: 'pre-wrap',
  });
  const pick = Object.assign(document.createElement('button'), { textContent: 'Choose export manifest...' });
  const close = Object.assign(document.createElement('button'), { textContent: '×', onclick: () => box.remove() });
  close.style.marginLeft = '8px';
  const status = document.createElement('div');
  status.style.marginTop = '6px';
  const input = Object.assign(document.createElement('input'), { type: 'file', accept: '.json,application/json' });
  input.style.display = 'none';
  box.append(pick, close, status, input);
  document.body.append(box);
  const show = (msg) => { status.textContent = msg; log(msg); };

  pick.onclick = () => input.click();
  input.onchange = async () => {
    const file = input.files[0];
    input.value = '';
    if (!file) return;

    let manifest;
    try { manifest = JSON.parse(await file.text()); }
    catch (e) { return show(`${file.name} is not valid JSON: ${e.message}`); }

    let items = (manifest.data_files || [])
      .filter((f) => f && f.export_url)
      .sort((a, b) => (a.batch_index ?? 0) - (b.batch_index ?? 0));
    if (ONLY_FILES.length) items = items.filter((f) => ONLY_FILES.includes(f.filename));
    if (!items.length) return show('No matching data_files with an export_url in this manifest.');

    // Only call claude.ai, so a malformed or tampered manifest can't send your session elsewhere.
    const bad = items.filter((f) => !signedUrlEndpoint(f.export_url));
    if (bad.length) { console.table(bad); return show(`Stopped: ${bad.length} link(s) are not claude.ai export links.`); }

    if (manifest.created_at) {
      const expires = Date.parse(manifest.created_at) + CFG.linkTtlMs;
      if (Date.now() > expires) return show(`Links expired ${new Date(expires).toLocaleString()}. Request a new export.`);
    }

    pick.disabled = true;
    const results = [];
    for (const [i, item] of items.entries()) {
      show(`[${i + 1}/${items.length}] ${item.filename}...`);
      try {
        results.push({ file: item.filename, result: await downloadOne(item) });
      } catch (e) {
        results.push({ file: item.filename, result: `FAILED: ${e.message}` });
        warn(`${item.filename}: ${e.message}`);
      }
      if (i < items.length - 1) await sleep(CFG.delayMs);
    }
    pick.disabled = false;
    console.table(results);
    const failed = results.filter((r) => r.result.startsWith('FAILED')).length;
    show(`Done: ${results.length - failed} of ${results.length} files. ${failed ? 'See the Console for failures.' : 'Confirm in chrome://downloads.'}`);
  };

  show('Click the button and pick a manifest-*.json file.');
})();
