/* StudyMate — home + continuous reader + collapsible Sage drawer. Vanilla JS. */
(() => {
  'use strict';
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  /* ---- who is this device? ------------------------------------------------
     Every browser keeps its own random id, so the shared library can say who
     added a file and offer an "only mine" view. */
  const VISITOR = (() => {
    let v = localStorage.getItem('sm2.visitor') || '';
    if (!/^[a-f0-9]{8,32}$/.test(v)) {
      v = [...crypto.getRandomValues(new Uint8Array(12))]
        .map((b) => b.toString(16).padStart(2, '0')).join('');
      localStorage.setItem('sm2.visitor', v);
    }
    return v;
  })();
  const deviceName = () => localStorage.getItem('sm2.device') || '';
  const realFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    if (url.startsWith('/api')) {
      const h = new Headers(init.headers || {});
      h.set('X-SM-Visitor', VISITOR);
      const dn = deviceName();
      if (dn) h.set('X-SM-Device', encodeURIComponent(dn));
      init = { ...init, headers: h };
    }
    return realFetch(input, init);
  };

  const state = {
    doc: null,
    pages: [],
    view: 'original',
    zoom: 1,
    busy: false,
    attach: null,          // { kind:'text'|'region', text, page, regions:[…] }
    regionMode: false,
    region: null,          // { page, box, x0f, y0f, x1f, y1f, text, image_url, region_id }
    find: { term: '', hits: [], i: -1 },
    settings: loadSettings(),
    threads: {},           // docId -> [{role, content}]
    tab: 'chat',
    ws: [],
    wsDoc: null,               // workspace is scoped to the open material
    providers: [],             // provider catalogue from the server
    ai: { site: false },       // does this site bring its own key?                // workspace files
  };

  /* ------------------------------------------------------------------ settings */
  function loadSettings() {
    let s = {};
    try { s = JSON.parse(localStorage.getItem('studymate.settings') || '{}'); } catch (e) { s = {}; }
    return {
      provider: s.provider || 'gemini', key: s.key || '', model: s.model || '',
      base_url: s.base_url || '', level: s.level || 'student', theme: s.theme || 'light',
    };
  }
  function saveSettings() {
    localStorage.setItem('studymate.settings', JSON.stringify(state.settings));
    fillSettingsForm(); paintKeyState();
  }

  /* ---------------- drawer width (drag to resize, remembered) ---------------- */
  const DRAWER_MIN = 300;
  const drawerMax = () => Math.max(DRAWER_MIN + 40, Math.min(window.innerWidth * 0.8, window.innerWidth - 300));

  function setDrawerWidth(px, persist) {
    const w = Math.max(DRAWER_MIN, Math.min(drawerMax(), Math.round(px)));
    document.documentElement.style.setProperty('--drawer', w + 'px');
    state.drawerWidth = w;
    if (persist) localStorage.setItem('studymate.drawerWidth', String(w));
    if (state.region) showRegionBar();
  }

  function resetDrawerWidth() { setDrawerWidth(window.innerWidth * 0.5, true); }

  (function initDrawerWidth() {
    const saved = parseInt(localStorage.getItem('studymate.drawerWidth') || '', 10);
    if (saved && saved >= DRAWER_MIN) setDrawerWidth(saved, false);
    else resetDrawerWidth();
  })();

  function initGrip() {
    const grip = $('#grip');
    const drawer = $('#drawer');
    let dragging = false;
    grip.addEventListener('pointerdown', (e) => {
      dragging = true; drawer.classList.add('resizing');
      grip.setPointerCapture(e.pointerId);
      document.body.style.userSelect = 'none';
      e.preventDefault();
    });
    window.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      setDrawerWidth(window.innerWidth - e.clientX, false);
    });
    window.addEventListener('pointerup', () => {
      if (!dragging) return;
      dragging = false; drawer.classList.remove('resizing');
      document.body.style.userSelect = '';
      localStorage.setItem('studymate.drawerWidth', String(state.drawerWidth));
    });
    grip.addEventListener('dblclick', resetDrawerWidth);
    window.addEventListener('resize', () => {
      if (state.drawerWidth) setDrawerWidth(state.drawerWidth, false);
    });
  }
  const applyTheme = () => { document.documentElement.dataset.theme = state.settings.theme; };

  /* ------------------------------------------------------------------ helpers */
  function toast(msg, ms = 2600) {
    const t = $('#toast');
    t.textContent = msg; t.classList.add('show');
    clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove('show'), ms);
  }
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const clip = (s, n = 140) => (String(s || '').length > n ? String(s).slice(0, n).trim() + '…' : String(s || ''));
  const fmtNum = (n) => (n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : String(n || 0));

  function inlineMd(s) {
    let t = esc(s);
    t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
    t = t.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    t = t.replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    t = t.replace(/(^|[\s(])_([^_\n]+)_/g, '$1<em>$2</em>');
    t = t.replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    t = t.replace(/\((?:p|pp)\.\s?(\d+)(?:[–-](\d+))?\)/g, (m, a, b) =>
      `<button class="pagepill" data-page="${b || a}">${m.slice(1, -1)}</button>`);
    return t;
  }

  function mdToHtml(md) {
    const lines = String(md || '').replace(/\r/g, '').split('\n');
    const out = [];
    let i = 0, inCode = false, codeBuf = [], listType = null, listBuf = [];
    const flush = () => {
      if (!listType) return;
      out.push(`<${listType}>` + listBuf.map((li) => `<li>${inlineMd(li)}</li>`).join('') + `</${listType}>`);
      listType = null; listBuf = [];
    };
    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        if (inCode) { out.push(`<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`); codeBuf = []; inCode = false; }
        else { flush(); inCode = true; }
        i++; continue;
      }
      if (inCode) { codeBuf.push(line); i++; continue; }
      if (/^\s*\|/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')) {
        flush();
        const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
        const head = cells(line);
        let j = i + 2; const rows = [];
        while (j < lines.length && /^\s*\|/.test(lines[j])) { rows.push(cells(lines[j])); j++; }
        out.push('<table><thead><tr>' + head.map((c) => `<th>${inlineMd(c)}</th>`).join('') + '</tr></thead><tbody>' +
          rows.map((r) => '<tr>' + r.map((c) => `<td>${inlineMd(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
        i = j; continue;
      }
      if (/^\s*$/.test(line)) { flush(); i++; continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
        flush(); const lvl = Math.min(m[1].length, 4);
        out.push(`<h${lvl}>${inlineMd(m[2])}</h${lvl}>`); i++; continue;
      }
      if (/^\s*([-*_])\s*\1\s*\1[\s-]*$/.test(line)) { flush(); out.push('<hr>'); i++; continue; }
      if ((m = line.match(/^\s*>\s?(.*)$/))) {
        flush();
        const buf = [m[1]]; let j = i + 1;
        while (j < lines.length && (m = lines[j].match(/^\s*>\s?(.*)$/))) { buf.push(m[1]); j++; }
        out.push(`<blockquote>${mdToHtml(buf.join('\n'))}</blockquote>`); i = j; continue;
      }
      if ((m = line.match(/^\s*(?:[-*+]|\u2022)\s+(.*)$/))) {
        if (listType !== 'ul') { flush(); listType = 'ul'; }
        listBuf.push(m[1]); i++; continue;
      }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        if (listType !== 'ol') { flush(); listType = 'ol'; }
        listBuf.push(m[1]); i++; continue;
      }
      flush();
      out.push(`<p>${inlineMd(line)}</p>`); i++;
    }
    flush();
    if (inCode) out.push(`<pre><code>${esc(codeBuf.join('\n'))}</code></pre>`);
    return out.join('\n');
  }

  function highlight(html, term) {
    if (!term) return html;
    const safe = esc(term).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    try { return html.replace(new RegExp(`(${safe})`, 'gi'), '<mark class="hitmark">$1</mark>'); }
    catch (e) { return html; }
  }

  /* ------------------------------------------------------------------ views */
  function showHome() {
    $('#viewer').hidden = true;
    $('#home').hidden = false;
    if ($('#libpage')) { $('#libpage').hidden = true; document.body.classList.remove('on-library'); }
    $('#filechip').hidden = true;
    document.body.classList.remove('drawer-open', 'region-mode');
    state.regionMode = false;
    $('#modehint').classList.remove('show');
    loadRecent();
  }

  /* Upload with progress + recovery.
     On some setups the answer to a big multipart POST can be lost even though the file
     was stored (that is exactly what "I can't upload anything" looked like). So: upload
     with progress, and if anything after the POST goes wrong, look the document up in
     the library by diffing it against the list taken just before the upload. */
  async function listIds() {
    try {
      const { docs } = await (await fetch('/api/docs', { cache: 'no-store' })).json();
      return new Set(docs.map((d) => d.id));
    } catch (e) { return new Set(); }
  }

  async function uploadFile(file) {
    if (!file) return;
    if (!/\.(pdf|docx?|pptx?|txt|md|markdown|rst|tex|log|json|ya?ml|csv|tsv|xlsx?|png|jpe?g|webp|gif|bmp|py|js|jsx|ts|tsx|java|c|h|cpp|cs|go|rs|rb|php|sql|sh|html|css|xml|ipynb)$/i.test(file.name)) {
      toast("That file type isn't supported yet."); return;
    }
    if (file.size > 60 * 1024 * 1024) { toast('File is larger than 60 MB.'); return; }

    const before = await listIds();
    showLoading(`Uploading ${file.name}… 0%`);

    await new Promise((resolve) => {
      const xhr = new XMLHttpRequest();
      const finish = () => resolve();
      xhr.open('POST', '/api/upload');
      xhr.responseType = 'text';
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round((e.loaded / e.total) * 100);
        showLoading(pct < 100 ? `Uploading ${file.name}… ${pct}%` : 'Reading and indexing your file…');
      };
      xhr.onload = async () => {
        let meta = null;
        try { meta = JSON.parse(xhr.responseText || '{}'); } catch (e) { meta = null; }
        if (xhr.status >= 400) {
          hideLoading();
          toast((meta && meta.detail) || `Could not read this file (HTTP ${xhr.status}).`, 5000);
          return finish();
        }
        if (!meta || !meta.id) { await recover(file, before); return finish(); }
        await openById(meta.id);
        finish();
      };
      xhr.onerror = async () => { await recover(file, before); finish(); };
      xhr.ontimeout = xhr.onerror;
      const fd = new FormData();
      fd.append('file', file);
      try { xhr.send(fd); } catch (e) { recover(file, before).then(finish); }
    });
  }

  /* the file may well have been stored even if we never saw the response */
  async function recover(file, before) {
    showLoading('Checking your library…');
    try {
      const { docs } = await (await fetch('/api/docs', { cache: 'no-store' })).json();
      const fresh = docs.filter((d) => !before.has(d.id));
      const mine = fresh.find((d) => d.filename === file.name)
        || (fresh.length === 1 ? fresh[0] : null);
      if (mine) {
        toast('Your file was stored — opening it.', 3000);
        await openById(mine.id);
        return;
      }
    } catch (e) { /* fall through */ }
    hideLoading();
    toast('Upload failed — the server never received the file. Please try again.', 6000);
  }

  async function openById(id) {
    showLoading('Opening…');
    try {
      const r = await fetch('/api/docs/' + id + '/full', { cache: 'no-store' });
      if (!r.ok) throw new Error('Could not open that document');
      openDoc(await r.json());
    } catch (e) {
      toast(e.message || 'Could not open that file', 5000);
    } finally { hideLoading(); }
  }

  function openDoc(doc) {
    const keepDrawer = drawerOpen();          // keep the agent open when swapping files
    state.doc = doc;
    state.pages = doc.pages || [];
    state.zoom = 1;
    state.attach = null; state.region = null;
    state.find = { term: '', hits: [], i: -1 };
    setRegionMode(false);
    clearAttach();
    setDrawer(keepDrawer);

    $('#home').hidden = true;
    $('#viewer').hidden = false;
    if ($('#libpage')) { $('#libpage').hidden = true; document.body.classList.remove('on-library'); }
    $('#filechip').hidden = false;
    $('#fname').textContent = doc.filename;
    $('#fmeta').textContent = `${doc.n_pages} ${doc.n_pages === 1 ? 'page' : 'pages'} · ${fmtNum(doc.n_words)} words`;
    $('#pagetotal').textContent = doc.n_pages;
    $('#jump').innerHTML = (doc.outline || []).map((o) =>
      `<option value="${o.n}">${esc(o.label)}${o.title ? ' — ' + esc(clip(o.title, 52)) : ''}</option>`).join('');
    $('#viewseg').hidden = !(doc.kind === 'paged' || doc.kind === 'sheet');
    $('#btn-region').hidden = !hasPageImages();
    $('#find').value = '';
    $('#findcount').textContent = '';
    $('#find').disabled = !doc.has_text;
    $('#find').placeholder = doc.has_text ? 'Find in file  ⌘K' : 'no text to search';

    renderPages();
    paintWsHead();
    restoreThread();
    paintKeyState();
    if (state.pages.some((p) => !(p.text || '').trim() && p.image_url)) {
      setTimeout(() => ocrPages(doc.id, 2).then(() => { if (state.doc && state.doc.id === doc.id) renderPages(); }), 400);
    }
    if (!document.hidden) loadWorkspace();
    $('#stage').scrollTop = 0;
    if (!doc.has_text && doc.kind !== 'image') {
      toast('This file has no text layer — Sage will need an API key to read it visually.', 4200);
    }
  }

  const hasPageImages = () => state.pages.some((p) => !!p.image_url);


  /* --------------------------------------------------- selectable text over page images */
  const wordCache = new Map();

  async function fillTextLayer(pageEl) {
    if (pageEl.dataset.layer === 'done' || pageEl.dataset.layer === 'loading') return;
    const n = parseInt(pageEl.dataset.page, 10);
    const box = pageEl.querySelector('.textlayer');
    if (!box || !state.doc) return;
    pageEl.dataset.layer = 'loading';
    try {
      let data = wordCache.get(`${state.doc.id}:${n}`);
      if (!data) {
        const r = await fetch(`/api/docs/${state.doc.id}/words/${n}`, { cache: 'force-cache' });
        data = r.ok ? await r.json() : { words: [] };
        wordCache.set(`${state.doc.id}:${n}`, data);
      }
      if (!data.words || !data.words.length || !box.isConnected) { pageEl.dataset.layer = 'empty'; return; }
      const pw = pageEl.clientWidth || 1, ph = pageEl.clientHeight || 1;
      if (!ph || pw < 2) { pageEl.dataset.layer = ''; return; }
      const sans = getComputedStyle(document.body).fontFamily || 'sans-serif';
      const ctx = (fillTextLayer.ctx ||= document.createElement('canvas').getContext('2d'));
      const frag = document.createDocumentFragment();
      data.words.forEach(([x, y, w, h, text]) => {
        const sp = document.createElement('span');
        sp.textContent = text + ' ';   // copied text keeps its word breaks
        sp.style.left = (x * 100).toFixed(3) + '%';
        sp.style.top = (y * 100).toFixed(3) + '%';
        // font size in container-width units keeps the layer correct at any zoom
        const px = Math.max(h * ph, 1);
        sp.style.fontSize = ((px / pw) * 100).toFixed(3) + 'cqw';
        // squeeze the glyphs so they match the real word box (makes highlights land on the right letters)
        ctx.font = `${px}px ${sans}`;
        const nat = ctx.measureText(text).width || 1;
        sp.style.transform = `scaleX(${Math.min(Math.max((w * pw) / nat, 0.05), 6).toFixed(3)})`;
        frag.appendChild(sp);
      });
      box.appendChild(frag);
      pageEl.dataset.layer = 'done';
    } catch (e) { pageEl.dataset.layer = 'error'; }
  }

  let layerObserver = null;
  function buildTextLayers() {
    const els = $$('#pages .page');
    if (layerObserver) layerObserver.disconnect();
    layerObserver = new IntersectionObserver((entries) => {
      entries.forEach((en) => { if (en.isIntersecting) fillTextLayer(en.target); });
    }, { root: $('#stage'), rootMargin: '600px 0px' });
    els.forEach((el) => layerObserver.observe(el));
    // anything already on screen right away
    els.slice(0, 3).forEach(fillTextLayer);
  }

  /* ------------------------------------------------------------------ reader */
  function renderPages() {
    const pages = $('#pages');
    const doc = state.doc;
    pages.style.setProperty('--zoom', state.zoom);
    pages.innerHTML = '';
    $('#emptyend').hidden = doc.n_pages < 2;

    const images = state.view === 'original' && hasPageImages();
    if (images) {
      state.pages.forEach((p) => {
        const el = document.createElement('div');
        el.className = 'page';
        el.dataset.page = p.n;
        el.innerHTML = `<img class="big" src="${p.image_url}" alt="${esc(p.label)}" loading="lazy" decoding="async" draggable="false">
                        <div class="textlayer" aria-hidden="true"></div>
                        <span class="num">${p.n}</span>`;
        el.querySelector('img').addEventListener('load', () => trackScroll());
        pages.appendChild(el);
      });
      afterImages();
      buildTextLayers();
      return;
    }

    // tables render as one block per sheet
    if (state.pages.some((p) => p.table)) {
      state.pages.forEach((p) => {
        const el = document.createElement('div');
        el.className = 'flow';
        el.dataset.page = p.n;
        el.innerHTML = p.table && p.table.length ? tableHtml(p.table) : '<p><em>Empty sheet.</em></p>';
        pages.appendChild(el);
      });
      trackScroll();
      return;
    }

    // text-like documents: one long sheet, section separators for multi-part files
    const el = document.createElement('div');
    el.className = 'flow';
    el.id = 'flowdoc';
    el.dataset.page = 1;
    const blocks = state.pages.map((p) => {
      const body = doc.kind === 'paged' || doc.kind === 'code'
        ? (doc.kind === 'code' ? `<pre><code>${esc(p.text)}</code></pre>` : ptext(p.text))
        : mdToHtml(p.text || '');
      return `<section data-page="${p.n}" class="pgsection">
        ${state.pages.length > 1 ? `<div class="sep" id="pg-${p.n}">${esc(p.label)}</div>` : ''}
        ${body || '<p><em>(no text on this page)</em></p>'}
      </section>`;
    }).join('');
    el.innerHTML = renderFiltered(blocks);
    pages.appendChild(el);
    bindSections();
    trackScroll();
  }

  function renderFiltered(blocks) {
    return state.find.term ? highlight(blocks, state.find.term) : blocks;
  }

  function ptext(text) {
    const paras = (text || '').split(/\n{2,}/).filter((x) => x.trim());
    return '<div class="ptext">' + (paras.length ? paras.map((x) => `<p>${esc(x.trim())}</p>`).join('')
      : '<p>(no text on this page)</p>') + '</div>';
  }

  function tableHtml(rows) {
    const head = rows[0], body = rows.slice(1, 500);
    let html = '<table><thead><tr>' + head.map((c) => `<th>${esc(c)}</th>`).join('') + '</tr></thead><tbody>' +
      body.map((r) => '<tr>' + r.map((c) => `<td>${esc(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>';
    if (rows.length > 501) html += `<p><em>Showing the first 500 of ${rows.length} rows.</em></p>`;
    return html;
  }

  function afterImages() {
    // draw any saved region overlay back on top of the image it belongs to
    if (state.region) paintRegionBox();
    trackScroll();
  }

  function bindSections() {
    // nothing needed yet; kept for symmetry / future per-section affordances
  }

  /* current page tracking + outline sync */
  let scrollRaf = null;
  function trackScroll() {
    const stage = $('#stage');
    if (scrollRaf) cancelAnimationFrame(scrollRaf);
    scrollRaf = requestAnimationFrame(() => {
      const els = $$('#pages .page, #pages .flow, #pages .pgsection');
      const mid = stage.getBoundingClientRect().top + stage.clientHeight * 0.3;
      let current = 1, best = Infinity, lastVisible = null;
      els.forEach((el) => {
        const r = el.getBoundingClientRect();
        const d = Math.abs(r.top - mid);
        if (d < best) { best = d; current = parseInt(el.dataset.page || '1', 10); lastVisible = el; }
        el.classList.toggle('near', r.top < stage.clientHeight && r.bottom > 0);
      });
      $('#pageind').textContent = current;
      if ($('#jump').value !== String(current)) $('#jump').value = String(current);
      state.currentPage = current;
    });
  }

  function goToPage(n, opts = {}) {
    n = Math.max(1, Math.min(state.doc.n_pages, n));
    const el = $(`#pages [data-page="${n}"]`);
    if (el) {
      el.scrollIntoView({ behavior: opts.smooth === false ? 'auto' : 'smooth', block: 'start' });
      if (opts.flash !== false) { el.classList.add('flash'); setTimeout(() => el.classList.remove('flash'), 1200); }
    } else { $('#stage').scrollTop = 0; }
    $('#pageind').textContent = n;
    $('#jump').value = String(n);
  }

  if ('ResizeObserver' in window) {
    new ResizeObserver(() => trackScroll()).observe(document.getElementById('pages'));
  }

  /* ------------------------------------------------------------------ drawer */
  function setTab(tab) {
    if (tab === 'files') { paintWsHead(); loadWorkspace(); }
    state.tab = tab;
    $$('#drawer-tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === tab)));
    $('#pane-chat').hidden = tab !== 'chat';
    $('#pane-files').hidden = tab !== 'files';
    if (tab === 'files') loadWorkspace();
  }

  function setDrawer(open) {
    document.body.classList.toggle('drawer-open', open);
    $('#drawer').setAttribute('aria-hidden', String(!open));
    if (open) {
      $('#agentbadge').hidden = true;
      $('#pop').classList.remove('show');
      if (state.region) showRegionBar();
      if (!$('#ask').value) setTimeout(() => $('#ask').focus(), 260);
    }
  }
  const drawerOpen = () => document.body.classList.contains('drawer-open');

  function paintKeyState() {
    const own = !!state.settings.key;
    const site = !!(state.ai && state.ai.site);
    const keyed = own || site;
    const prov = own ? state.settings.provider : (state.ai.provider || '');
    const info = provInfo(prov);
    const model = own ? (state.settings.model || info.model || '')
      : (state.ai.model || info.model || '');
    const dot = $('#modedot');
    dot.className = 'dot ' + (keyed ? 'on' : 'offline');
    $('#modesub').textContent = keyed
      ? `${own ? prov : 'site key'} · ${model || 'default'}`
      : 'offline mode · add a key';
    $('#keylabel').textContent = own ? 'AI key ✓' : site ? 'AI ready ✓' : 'Add AI key';
    $('#hintright').textContent = keyed
      ? 'Enter to send'
      : 'offline — extractive answers';
    const nudge = $('#keynudge');
    if (nudge) nudge.hidden = keyed;
    if ($('#sitekey-state')) paintSiteKey();
  }

  /* ------------------------------------------------------- site key panel */
  function paintSiteKey() {
    const ai = state.ai || {};
    const stateEl = $('#sitekey-state');
    const body = $('#sitekey-body');
    if (!stateEl || !body) return;
    if (ai.site) {
      const info = provInfo(ai.provider);
      const bk = (ai.backups || []).map((b) => (provInfo(b.provider).label || b.provider).split(' —')[0]);
      stateEl.textContent = `Active · ${info.label || ai.provider}${ai.model ? ' · ' + ai.model : ''}`
        + (bk.length ? ` → backup: ${bk.join(', ')}` : '');
      if (ai.keys && ai.keys.length) {
        const resting = ai.keys.filter((k) => k.state === 'resting');
        if (resting.length) {
          stateEl.textContent += ` · ${resting.length} resting (` +
            resting.map((k) => `${k.provider} ${k.minutes}m`).join(', ') + ')';
        }
      }
      stateEl.className = 'on';
      body.classList.add('configured');
      if (ai.managed) {
        body.querySelectorAll('input,select,#sk-save,#sk-clear').forEach((el) => { el.disabled = true; });
        stateEl.textContent += ' · managed by the server';
      } else {
        body.querySelectorAll('input,select,#sk-save,#sk-clear').forEach((el) => { el.disabled = false; });
      }
      $('#skprov').value = ai.provider || 'gemini';
      $('#smodel').value = ai.model || '';
      if ($('#sbase-field')) $('#sbase-field').hidden = $('#skprov').value !== 'custom';
    } else {
      stateEl.textContent = 'Not set — visitors must add their own key';
      stateEl.className = '';
      body.classList.remove('configured');
      body.querySelectorAll('input,select,#sk-save,#sk-clear').forEach((el) => { el.disabled = false; });
    }
  }

  async function saveSiteKey() {
    const st = $('#keystatus');
    const btn = $('#sk-save');
    btn.disabled = true;
    try {
      const r = await fetch('/api/site-key', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: $('#skey').value.trim(), provider: $('#skprov').value,
          model: $('#smodel').value.trim() || undefined,
          base_url: $('#sbase').value.trim() || undefined, passcode: $('#spass').value,
          backups: (($('#skey2') && $('#skey2').value.trim())
            ? [{ provider: $('#skprov2').value, api_key: $('#skey2').value.trim() }] : []),
        }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Could not save the site key');
      state.ai = data;
      $('#skey').value = ''; $('#spass').value = '';
      paintKeyState();
      toast('Site key saved — everyone can use AI now', 3000);
      st.className = 'status ok show';
      st.textContent = 'Saved. Test it once to be sure the key works.';
    } catch (e) {
      st.className = 'status err show';
      st.textContent = e.message;
    } finally { btn.disabled = false; }
  }

  /* ------------------------------------------------------------------ thread */
  function addMsg(role, html) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + role;
    wrap.innerHTML = `<div class="av">${role === 'user' ? 'You' : 'S'}</div><div class="body">${html}</div>`;
    $('#thread').appendChild(wrap);
    scrollThread(true);
    return wrap.querySelector('.body');
  }

  /* "save this answer as a file" — Sage only writes to disk when you ask it to */
  function attachTools(body, text, mode) {
    if (!text || !text.trim()) return;
    const row = document.createElement('div');
    row.className = 'mtools';
    row.innerHTML = `<button data-save>＋ Save to workspace</button><button data-copy>Copy</button>`;
    row.querySelector('[data-save]').onclick = () => {
      const title = `${labelFor(mode)} — ${docBase()}`;
      saveToWorkspace(title, text, mode);
    };
    row.querySelector('[data-copy]').onclick = async () => {
      try { await navigator.clipboard.writeText(text); toast('Copied', 1400); }
      catch (e) { toast('Copy blocked by the browser'); }
    };
    body.appendChild(row);
  }

  function addNote(html) {
    const el = document.createElement('div');
    el.className = 'offline-note';
    el.innerHTML = html;
    $('#thread').appendChild(el);
    scrollThread(true);
    return el;
  }

  const isPinned = () => { const th = $('#thread'); return th.scrollHeight - th.scrollTop - th.clientHeight < 180; };
  function scrollThread(force) {
    const th = $('#thread');
    if (force || isPinned()) th.scrollTop = th.scrollHeight;
  }

  function threadFor(id) { return state.threads[id] || (state.threads[id] = []); }

  function restoreThread() {
    $('#thread').innerHTML = '';
    let saved = [];
    try { saved = JSON.parse(sessionStorage.getItem('sm2.thread.' + state.doc.id) || '[]'); } catch (e) { saved = []; }
    state.threads[state.doc.id] = saved;
    if (saved.length) {
      saved.forEach((m) => {
        const body = addMsg(m.role, m.role === 'user' ? esc(m.content) : mdToHtml(m.content));
        if (m.role === 'assistant') attachTools(body, m.content, m.mode);
      });
      $('#agentbadge').hidden = false;
      scrollThread(true);
    } else {
      const doc = state.doc;
      addMsg('assistant', mdToHtml(
        `**${doc.filename}** is open — ${doc.n_pages} ${doc.n_pages === 1 ? 'page' : 'pages'}, ${fmtNum(doc.n_words)} words.\n\n` +
        `Whenever something is unclear:\n` +
        `- **Highlight any sentence** in the document and press *Explain* — or *Ask Sage*.\n` +
        `- Press the **Select area** button on the right and drag a box around a diagram, table or paragraph.\n` +
        `- Or ask me directly: "what does section 3 actually mean?"`));
    }
  }

  function persistThread() {
    if (!state.doc) return;
    try {
      sessionStorage.setItem('sm2.thread.' + state.doc.id, JSON.stringify(threadFor(state.doc.id)));
    } catch (e) { /* quota */ }
  }

  /* ------------------------------------------------------------------ attach */
  function setAttach(a) {
    state.attach = a;
    const box = $('#attach');
    if (!a) { box.classList.remove('show'); return; }
    box.classList.add('show');
    const thumb = $('#atchthumb');
    if (a.image_url) thumb.innerHTML = `<img src="${a.image_url}" alt="selection" />`;
    else thumb.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 7h16M4 12h10M4 17h7"/></svg>`;
    $('#atchtitle').textContent = a.kind === 'region' ? `Page ${a.page} · selected area` : `Page ${a.page} · selected passage`;
    $('#atchsub').textContent = clip((a.text || '').replace(/\s+/g, ' ') || 'image region', 90);
  }
  function clearAttach() { setAttach(null); }

  /* ------------------------------------------------------------------ ask */
  function attachPayload() {
    const a = state.attach;
    if (!a) return { selection: '', regions: [] };
    if (a.kind === 'region') return { selection: '', regions: a.regions || [] };
    return { selection: a.text || '', regions: [] };
  }

  async function askTutor({ mode = 'chat', question = '', label = null } = {}) {
    if (!state.doc) { toast('Open a file first.'); return; }
    if (state.busy) { toast('Sage is still answering — one moment.'); return; }
    state.busy = true; $('#send').disabled = true;
    setDrawer(true);

    const shown = label || question || {
      explain: 'Explain this to me.', summary: 'Summarise this material.', concepts: 'What are the key concepts?',
      quiz: 'Quiz me on this.', glossary: 'Build a glossary.', flashcards: 'Make flashcards.',
    }[mode] || 'Help me with this.';
    addMsg('user', esc(shown));

    const body = addMsg('assistant', '<span class="typing"><i></i><i></i><i></i></span>');
    let buffer = '', meta = { mode: state.settings.key ? 'llm' : 'offline' }, raf = null;

    const paint = () => {
      raf = null;
      const pin = isPinned();
      body.innerHTML = mdToHtml(buffer) || '<span class="typing"><i></i><i></i><i></i></span>';
      if (pin) scrollThread(true);
    };

    const { selection, regions } = attachPayload();
    try {
      const res = await fetch('/api/tutor', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          doc_id: state.doc.id, mode, question, selection, regions,
          history: threadFor(state.doc.id).slice(-8),
          provider: state.settings.provider, api_key: state.settings.key,
          model: state.settings.model || undefined, base_url: state.settings.base_url || undefined,
          level: state.settings.level,
        }),
      });
      if (!res.ok || !res.body) throw new Error((await res.json().catch(() => ({}))).detail || 'Tutor request failed');

      const reader = res.body.getReader(), dec = new TextDecoder();
      let raw = '';
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        raw += dec.decode(value, { stream: true });
        const frames = raw.split('\n\n');
        raw = frames.pop() || '';
        for (const f of frames) {
          const ev = (f.match(/^event: (.*)$/m) || [])[1];
          const line = (f.match(/^data: (.*)$/m) || [])[1];
          if (!line) continue;
          let data = {};
          try { data = JSON.parse(line); } catch (e) { continue; }
          if (ev === 'meta') Object.assign(meta, data);
          else if (ev === 'delta') { buffer += data.text; if (!raf) raf = requestAnimationFrame(paint); }
          else if (ev === 'error') throw new Error(data.message || 'Provider error');
        }
      }
      if (raf) cancelAnimationFrame(raf);
      paint();

      if (meta.mode === 'offline' && !state.settings.key) {
        const note = addNote('Offline mode — this is quoted from your file, not reasoned about. ' +
          '<a id="offkey">Add an API key</a> for a real conversation.');
        note.querySelector('#offkey').onclick = () => openModal('settings');
      }
      attachTools(body, buffer, mode);
      const t = threadFor(state.doc.id);
      t.push({ role: 'user', content: shown }, { role: 'assistant', content: buffer, mode });
      persistThread();
      if (!drawerOpen()) $('#agentbadge').hidden = false;
      clearAttach();
    } catch (e) {
      body.innerHTML = `<div class="offline-note">${esc(e.message || 'Something went wrong.')}</div>`;
    } finally {
      state.busy = false; $('#send').disabled = false;
    }
  }


  /* ------------------------------------------------------------------ workspace */
  const wsIco = (it) => {
    if (it.source === 'sage') return 'sage';
    const e = (it.ext || '').toLowerCase();
    if (e === '.pdf') return 'pdf';
    if (['.csv', '.tsv', '.xlsx'].includes(e)) return 'sheet';
    if (['.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'].includes(e)) return 'img';
    if (['.docx', '.pptx'].includes(e)) return 'doc';
    return 'doc';
  };
  const wsTag = (it) => (it.source === 'sage' ? 'Sage' : it.source === 'workspace' ? 'Added' : 'Uploaded');
  const wsSize = (n) => (n > 1048576 ? (n / 1048576).toFixed(1) + ' MB' : n > 1024 ? Math.round(n / 1024) + ' KB' : (n || 0) + ' B');
  const wsWhen = (t) => {
    if (!t) return '';
    const s = Math.max(0, (Date.now() / 1000) - t);
    if (s < 90) return 'just now';
    if (s < 3600) return Math.round(s / 60) + ' min ago';
    if (s < 86400) return Math.round(s / 3600) + ' h ago';
    return new Date(t * 1000).toLocaleDateString();
  };

  async function loadWorkspace() {
    const list = $('#wslist');
    const did = state.doc ? state.doc.id : null;
    state.wsDoc = did;
    if (!list.dataset.ready) list.innerHTML = '<div class="wsp-empty">Loading…</div>';
    try {
      const q = did ? '?doc_id=' + encodeURIComponent(did) : '';
      const { files } = await (await fetch('/api/workspace' + q, { cache: 'no-store' })).json();
      if (did !== state.wsDoc) return;          // a newer document took over while this was in flight
      state.ws = files || [];
      renderWorkspace();
    } catch (e) {
      list.innerHTML = '<div class="wsp-empty">Could not load the workspace.</div>';
    }
    list.dataset.ready = '1';
  }

  function paintWsHead() {
    const name = state.doc ? state.doc.filename : null;
    const title = $('#wsp-doc'), sub = $('#wsp-sub');
    if (!title) return;
    title.textContent = name ? `Workspace · ${name}` : 'Workspace';
    sub.textContent = name
      ? 'Files, notes and Sage output for this material'
      : 'Open a material to see its own workspace';
  }

  function renderWorkspace() {
    const list = $('#wslist');
    const files = state.ws;
    const count = $('#wscount');
    if (count) { count.textContent = String(files.length); count.hidden = !files.length; }
    if (!files.length) {
      const nm = state.doc ? esc(state.doc.filename) : 'this material';
      list.innerHTML = `<div class="wsp-empty"><b>Nothing here yet</b>
        Add a file, pull one from your library, or ask Sage something — notes it writes for
        ${nm} land here and stay with this material.</div>`;
      return;
    }
    list.innerHTML = files.map((it) => `
      <div class="wsp-item" data-id="${it.id}">
        <span class="wsp-ico ${wsIco(it)}">${esc((it.ext || 'file').replace('.', '').slice(0, 4).toUpperCase() || 'FILE')}</span>
        <span class="wsp-meta">
          <b title="${esc(it.name)}">${esc(it.name)}</b>
          <span><em class="tag">${wsTag(it)}</em> ${wsSize(it.size)}${it.pages ? ' · ' + it.pages + 'p' : ''} · ${wsWhen(it.created)}</span>
        </span>
        <span class="wsp-btns">
          <button data-act="open" title="Open in reader"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H19v15H6.5A2.5 2.5 0 0 0 4 20.5z"/></svg></button>
          <button data-act="download" title="Download"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="M7 10l5 5 5-5"/><path d="M12 15V3"/></svg></button>
          <button data-act="del" class="del" title="Remove from workspace"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg></button>
        </span>
      </div>`).join('');

    list.querySelectorAll('.wsp-item').forEach((row) => {
      const it = files.find((f) => f.id === row.dataset.id);
      row.querySelector('[data-act="open"]').onclick = () => openWorkspaceFile(it);
      row.querySelector('[data-act="download"]').onclick = () => {
        window.open('/api/workspace/' + it.id + '/raw', '_blank');
      };
      row.querySelector('[data-act="del"]').onclick = async () => {
        row.style.opacity = '.4';
        await fetch('/api/workspace/' + it.id, { method: 'DELETE' });
        state.ws = state.ws.filter((f) => f.id !== it.id);
        renderWorkspace();
        toast('Removed from workspace');
      };
    });
  }

  async function openWorkspaceFile(it) {
    const textish = ['.md', '.markdown', '.txt', '.csv', '.json'].includes((it.ext || '').toLowerCase());
    if (textish) {
      try {
        const r = await fetch('/api/workspace/' + it.id + '/text');
        if (r.ok) {
          const { text } = await r.json();
          $('#pv-name').textContent = it.name;
          $('#pv-body').textContent = text;
          $('#pv-download').onclick = () => window.open('/api/workspace/' + it.id + '/raw', '_blank');
          $('#pv-open').onclick = async () => {
            closeModal('preview');
            await readerFromWorkspace(it);
          };
          openModal('preview');
          return;
        }
      } catch (e) { /* fall through to the reader */ }
    }
    await readerFromWorkspace(it);
  }

  async function readerFromWorkspace(it) {
    showLoading('Opening in the reader…');
    try {
      const r = await fetch(`/api/workspace/${it.id}/open`, { method: 'POST' });
      if (!r.ok) throw new Error('Could not open that file');
      const { doc_id } = await r.json();
      await openById(doc_id);
      loadWorkspace();
    } catch (e) { toast(e.message, 4000); } finally { hideLoading(); }
  }

  async function saveToWorkspace(title, text, mode) {
    if (!text || !text.trim()) { toast('Nothing to save yet.'); return; }
    try {
      const r = await fetch('/api/workspace/from-text', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, text, doc_id: state.doc ? state.doc.id : null, mode }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Could not save');
      const entry = await r.json();
      state.ws.unshift(entry);
      renderWorkspace();
      toast('Saved to your workspace', 2200);
    } catch (e) { toast(e.message, 4000); }
  }

  /* ------------------------------------------------------- AI key helpers */
  async function loadProviders() {
    try {
      const { providers } = await (await fetch('/api/providers', { cache: 'no-store' })).json();
      state.providers = providers || [];
    } catch (e) { state.providers = []; }
    return state.providers;
  }

  const provInfo = (id) => state.providers.find((p) => p.id === id)
    || { id, label: id, model: '', base_url: '', signup: '', note: '', free: false };

  async function loadAIStatus() {
    try {
      state.ai = await (await fetch('/api/ai-status', { cache: 'no-store' })).json();
    } catch (e) { state.ai = { site: false }; }
    paintKeyState();
    return state.ai;
  }

  function paintProviderHints() {
    const info = provInfo($('#provider').value);
    $('#model').placeholder = info.model || 'default';
    $('#baseurl').placeholder = info.base_url || 'http://localhost:11434/v1';
    const link = $('#getkey');
    if (info.signup) {
      link.href = info.signup;
      link.hidden = false;
      link.textContent = info.free ? 'Get a free key →' : 'Get an API key →';
    } else {
      link.hidden = true;
    }
    $('#provnote').textContent = info.note || '';
    const custom = info.kind === 'custom' || $('#provider').value === 'custom';
    $('#baseurl').closest('.field').style.opacity = custom ? '1' : '.55';
  }

  const docBase = () => (state.doc ? state.doc.filename.replace(/\.[^.]+$/, '') : 'note');
  const labelFor = (mode) => ({
    summary: 'Summary', concepts: 'Key concepts', glossary: 'Glossary', quiz: 'Quiz',
    flashcards: 'Flashcards', explain: 'Explanation', chat: 'Note',
  }[mode] || 'Note');

  /* ------------------------------------------------------------------ text selection */
  const pop = $('#pop');
  const hidePop = () => pop.classList.remove('show');

  document.addEventListener('mouseup', (e) => {
    if (pop.contains(e.target) || $('#regionbar').contains(e.target)) return;
    if (state.regionMode || !state.doc) return;
    setTimeout(() => {
      const sel = window.getSelection();
      const text = (sel ? sel.toString() : '').trim();
      const stage = $('#stage');
      if (!sel || text.length < 2 || !stage.contains(sel.anchorNode) || !stage.contains(sel.focusNode)) { hidePop(); return; }
      const pageEl = sel.anchorNode.parentElement.closest('[data-page]');
      state.pendingText = { text, page: pageEl ? parseInt(pageEl.dataset.page, 10) : (state.currentPage || 1) };
      const words = text.split(/\s+/).filter(Boolean).length;
      $('#plabel').textContent = `${words} ${words === 1 ? 'word' : 'words'} selected`;
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      pop.classList.add('show');
      const w = pop.offsetWidth, h = pop.offsetHeight;
      pop.style.left = Math.max(10, Math.min(readerRight() - w, rect.left + rect.width / 2 - w / 2)) + 'px';
      pop.style.top = Math.max(10, rect.top - h - 8) + 'px';
    }, 10);
  });

  document.addEventListener('mousedown', (e) => {
    if (!pop.contains(e.target)) hidePop();
  });

  pop.addEventListener('click', (e) => {
    const act = e.target.dataset.act;
    if (!act) return;
    if (act === 'clear') {
      hidePop();
      const sel = window.getSelection();
      if (sel) sel.removeAllRanges();
      state.pendingText = null;
      return;
    }
    if (!state.pendingText) return;
    hidePop();
    const { text, page } = state.pendingText;
    setAttach({ kind: 'text', text, page });
    if (act === 'ask') { setDrawer(true); $('#ask').focus(); return; }
    const map = {
      explain: { mode: 'explain', q: 'Explain this passage in plain language.' },
      simple: { mode: 'explain', q: 'Explain this as if I were a complete beginner, with a concrete example.' },
      quiz: { mode: 'quiz', q: 'Make 3 questions about this passage only.' },
      translate: { mode: 'explain', q: 'List and define the key terms in this passage.' },
    }[act];
    askTutor({ mode: map.mode, question: map.q, label: `${map.q}\n\n“${clip(text, 120)}”` });
  });

  /* ------------------------------------------------------------------ region select */
  const bar = $('#regionbar');

  function setRegionMode(on) {
    state.regionMode = on;
    document.body.classList.toggle('region-mode', on);
    $('#btn-region').classList.toggle('active', on);
    $('#modehint').classList.toggle('show', on);
    if (!on) { const p = $('.regionbox.pending'); if (p) p.remove(); }
  }

  $('#btn-region').onclick = () => {
    if (!hasPageImages()) { toast('Area select works on PDFs and images — for text, just highlight it.'); return; }
    if (state.view === 'text') { state.view = 'original'; syncSeg(); renderPages(); }
    setRegionMode(!state.regionMode);
    if (state.regionMode) toast('Drag a box around anything on the page', 2200);
  };
  $('#modecancel').onclick = () => setRegionMode(false);

  let drag = null;
  $('#pages').addEventListener('mousedown', (e) => {
    if (!state.regionMode || e.button !== 0) return;
    const page = e.target.closest('.page');
    const img = page && page.querySelector('img.big');
    if (!page || !img) return;
    e.preventDefault();
    clearRegion();
    const box = document.createElement('div');
    box.className = 'regionbox pending';
    page.appendChild(box);
    const r = page.getBoundingClientRect();
    drag = { page, img, box, rect: r, x0: e.clientX, y0: e.clientY };
    updateBox(e);
  });

  function updateBox(e) {
    if (!drag) return;
    const x = Math.min(Math.max(e.clientX, drag.rect.left), drag.rect.right) - drag.rect.left;
    const y = Math.min(Math.max(e.clientY, drag.rect.top), drag.rect.top + drag.rect.height) - drag.rect.top;
    const x0 = Math.min(x, drag.x0 - drag.rect.left), x1 = Math.max(x, drag.x0 - drag.rect.left);
    const y0 = Math.min(y, drag.y0 - drag.rect.top), y1 = Math.max(y, drag.y0 - drag.rect.top);
    Object.assign(drag.box.style, {
      left: x0 + 'px', top: y0 + 'px', width: (x1 - x0) + 'px', height: (y1 - y0) + 'px',
    });
    drag.coords = { x0, y0, x1, y1 };
  }

  window.addEventListener('mousemove', (e) => { if (drag) updateBox(e); });
  window.addEventListener('mouseup', async () => {
    if (!drag) return;
    const { page, img, box, coords } = drag;
    const w = coords ? coords.x1 - coords.x0 : 0;
    const h = coords ? coords.y1 - coords.y0 : 0;
    drag = null;
    if (w < 14 || h < 10) { box.remove(); return; }

    const nW = img.naturalWidth || img.clientWidth;
    const nH = img.naturalHeight || img.clientHeight;
    const sx = nW / img.clientWidth, sy = nH / img.clientHeight;
    const rectPx = [coords.x0 * sx, coords.y0 * sy, coords.x1 * sx, coords.y1 * sy];

    box.classList.remove('pending');
    box.remove();
    const frac = {
      x0f: coords.x0 / img.clientWidth, y0f: coords.y0 / img.clientHeight,
      x1f: coords.x1 / img.clientWidth, y1f: coords.y1 / img.clientHeight,
    };
    const pageNo = parseInt(page.dataset.page, 10);
    showLoading('Reading that area…');
    try {
      const r = await fetch(`/api/docs/${state.doc.id}/region`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ page: pageNo, rect: rectPx, zoom: nW / img.clientWidth }),
      });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Could not read that area');
      const reg = await r.json();
      state.region = { ...reg, page: pageNo, ...frac };
      paintRegionBox();
      showRegionBar();
      setRegionMode(false);
      const preview = reg.text ? clip(reg.text.replace(/\s+/g, ' '), 90) : 'no text layer — Sage will read the image';
      setAttach({ kind: 'region', page: pageNo, text: reg.text || preview, image_url: reg.image_url,
        regions: [{ region_id: reg.region_id, page: pageNo, text: reg.text || '' }] });
    } catch (e) {
      toast(e.message, 4000);
    } finally { hideLoading(); }
  });

  function paintRegionBox() {
    $$('.region-saved').forEach((x) => x.remove());
    const r = state.region;
    if (!r) return;
    const pageEl = $(`#pages .page[data-page="${r.page}"]`);
    if (!pageEl) return;
    const box = document.createElement('div');
    box.className = 'region-saved';
    Object.assign(box.style, {
      left: (r.x0f * 100) + '%', top: (r.y0f * 100) + '%',
      width: ((r.x1f - r.x0f) * 100) + '%', height: ((r.y1f - r.y0f) * 100) + '%',
    });
    pageEl.appendChild(box);
    state.region.el = box;
  }

  const readerRight = () => (drawerOpen() ? window.innerWidth - (state.drawerWidth || 0) - 12 : window.innerWidth - 10);

  function showRegionBar() {
    const r = state.region;
    if (!r || !r.el) return;
    const rect = r.el.getBoundingClientRect();
    bar.classList.add('show');
    const w = bar.offsetWidth, h = bar.offsetHeight;
    const right = readerRight();
    bar.style.left = Math.max(10, Math.min(right - w, rect.left + rect.width / 2 - w / 2)) + 'px';
    bar.style.top = (rect.top - h - 10 > 64 ? rect.top - h - 10 : Math.min(window.innerHeight - h - 12, rect.bottom + 10)) + 'px';
    $('#rlabel').textContent = `p.${r.page} · ${Math.round((r.x1f - r.x0f) * 100)}% × ${Math.round((r.y1f - r.y0f) * 100)}%`;
  }

  function clearRegion() {
    $$('.region-saved').forEach((x) => x.remove());
    state.region = null;
    bar.classList.remove('show');
    if (state.attach && state.attach.kind === 'region') clearAttach();
  }

  bar.addEventListener('click', (e) => {
    const act = e.target.dataset.ract;
    if (!act) return;
    if (act === 'clear') { clearRegion(); return; }
    if (!state.region) return;
    const r = state.region;
    setAttach({ kind: 'region', page: r.page, text: r.text, image_url: r.image_url,
      regions: [{ region_id: r.region_id, page: r.page, text: r.text || '' }] });
    if (act === 'ask') { bar.classList.remove('show'); setDrawer(true); $('#ask').focus(); return; }
    const map = {
      explain: { mode: 'explain', q: 'Explain what this highlighted region of the page shows.' },
      simple: { mode: 'explain', q: 'Explain this region as if I were a complete beginner.' },
      quiz: { mode: 'quiz', q: 'Make 2 questions about this region only.' },
    }[act];
    bar.classList.remove('show');
    askTutor({ mode: map.mode, question: map.q, label: `${map.q}  (p.${r.page} region)` });
  });

  window.addEventListener('scroll', () => { if (state.region) showRegionBar(); }, true);
  window.addEventListener('resize', () => { if (state.region) showRegionBar(); });

  /* ------------------------------------------------------------------ find */
  async function runFind(term, cycle) {
    state.find.term = term;
    if (!term) { state.find = { term: '', hits: [], i: -1 }; $('#findcount').textContent = ''; renderPages(); return; }
    if (!cycle || !state.find.hits.length) {
      const r = await fetch(`/api/docs/${state.doc.id}/search?q=${encodeURIComponent(term)}`);
      const d = await r.json();
      state.find.hits = d.hits || [];
      state.find.i = -1;
      if (state.pages.some((p) => p.image_url) && state.view === 'original') { /* images: page-level only */ }
      else renderPages();
    }
    if (!state.find.hits.length) { $('#findcount').textContent = 'no match'; return; }
    state.find.i = (state.find.i + 1) % state.find.hits.length;
    const hit = state.find.hits[state.find.i];
    $('#findcount').textContent = `${state.find.i + 1}/${state.find.hits.length}`;
    goToPage(hit.page);
  }

  /* ------------------------------------------------------------------ bindings */
  $('#brand').onclick = showHome;
  $('#btn-back').onclick = showHome;
  $('#btn-open').onclick = () => $('#fileinput').click();
  $('#btn-browse').onclick = () => $('#fileinput').click();
  $('#fileinput').onchange = (e) => { const f = e.target.files[0]; e.target.value = ''; uploadFile(f); };

  const dz = $('#dropzone');
  ['dragenter', 'dragover'].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add('hot'); }));
  ['dragleave', 'drop'].forEach((ev) => document.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove('hot'); }));
  document.addEventListener('drop', (e) => { const f = e.dataTransfer?.files?.[0]; if (f) uploadFile(f); });

  ['#btn-sample', '#btn-sample2'].forEach((sel) => {
    const b = $(sel);
    if (!b) return;
    b.onclick = async () => {
      try {
        showLoading('Loading the sample…');
        const r = await fetch('/static/samples/' + b.dataset.file);
        const blob = await r.blob();
        await uploadFile(new File([blob], b.dataset.title, { type: b.dataset.kind }));
      } catch (e) { toast('Could not load the sample'); } finally { hideLoading(); }
    };
  });

  $('#stage').addEventListener('scroll', trackScroll, { passive: true });
  $('#jump').onchange = (e) => goToPage(parseInt(e.target.value, 10) || 1);
  $('#zoomin').onclick = () => { state.zoom = Math.min(2.2, +(state.zoom + 0.15).toFixed(2)); renderPages(); };
  $('#zoomout').onclick = () => { state.zoom = Math.max(0.6, +(state.zoom - 0.15).toFixed(2)); renderPages(); };

  function syncSeg() {
    $$('#viewseg button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.view === state.view)));
  }
  $$('#viewseg button').forEach((b) => {
    b.onclick = () => { state.view = b.dataset.view; syncSeg(); renderPages(); };
  });

  $('#find').addEventListener('input', (e) => {
    const v = e.target.value.trim();
    if (v.length > 1) runFind(v, false);
    else { state.find = { term: '', hits: [], i: -1 }; $('#findcount').textContent = ''; renderPages(); }
  });
  $('#find').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); runFind($('#find').value.trim(), true); }
  });

  $('#btn-agent').onclick = () => setDrawer(!drawerOpen());
  $('#btn-collapse').onclick = () => setDrawer(false);

  $$('#chips .chip').forEach((c) => {
    c.onclick = () => {
      const mode = c.dataset.mode;
      if (mode === 'explain' && !state.attach) {
        const sel = (window.getSelection() || {}).toString().trim();
        if (sel.length > 9) {
          const pageEl = (window.getSelection().anchorNode?.parentElement || {}).closest?.('[data-page]');
          setAttach({ kind: 'text', text: sel, page: pageEl ? +pageEl.dataset.page : (state.currentPage || 1) });
        } else {
          setDrawer(true);
          toast('Highlight something first, or drag a box with Select area.', 3200);
          return;
        }
      }
      askTutor({ mode });
    };
  });

  const ask = $('#ask');
  ask.addEventListener('input', () => { ask.style.height = 'auto'; ask.style.height = Math.min(130, ask.scrollHeight) + 'px'; });
  ask.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitAsk(); }
  });
  $('#send').onclick = submitAsk;

  function submitAsk() {
    const q = ask.value.trim();
    if (!q) { if (!drawerOpen()) setDrawer(true); return; }
    ask.value = ''; ask.style.height = 'auto';
    askTutor({ mode: 'chat', question: q });
  }
  $('#attachclear').onclick = () => { clearAttach(); clearRegion(); };

  document.addEventListener('click', (e) => {
    const pill = e.target.closest('.pagepill');
    if (pill) { goToPage(parseInt(pill.dataset.page, 10)); }
  });

  /* ------------------------------------------------------------------ modals */
  const openModal = (id) => { $('#' + id).classList.add('show'); };
  const closeModal = (id) => $('#' + id).classList.remove('show');
  $$('[data-close]').forEach((b) => { b.onclick = () => closeModal(b.dataset.close); });
  $$('.scrim').forEach((s) => s.addEventListener('click', (e) => { if (e.target === s) s.classList.remove('show'); }));
  $('#ws-upload').onclick = () => $('#wsfile').click();
  $('#ws-fromlib').onclick = () => openPicker();
  $('#wsfile').onchange = async (e) => {
    const f = e.target.files[0];
    e.target.value = '';
    if (!f) return;
    showLoading(`Adding ${f.name}…`);
    try {
      const fd = new FormData();
      fd.append('file', f);
      if (state.doc) fd.append('doc_id', state.doc.id);
      const r = await fetch('/api/workspace/upload', { method: 'POST', body: fd });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || 'Could not add that file');
      state.ws.unshift(await r.json());
      renderWorkspace();
      toast('Added to workspace');
    } catch (err) { toast(err.message, 4000); } finally { hideLoading(); }
  };

  $('#ws-savechat').onclick = () => {
    const msgs = threadFor(state.doc ? state.doc.id : '');
    if (!msgs.length) { toast('Nothing to save yet.'); return; }
    const md = [`# Study notes — ${state.doc ? state.doc.filename : 'conversation'}`, '',
      ...msgs.map((m) => (m.role === 'user' ? `**Q:** ${m.content}\n` : m.content + '\n'))].join('\n');
    saveToWorkspace(`Study notes — ${docBase()}`, md, 'chat');
  };

  $$('#drawer-tabs button').forEach((b) => {
    b.onclick = () => setTab(b.dataset.tab);
  });

  $('#btn-settings').onclick = async () => {
    if (!state.providers.length) await loadProviders();
    await loadAIStatus();
    buildProviderOptions();
    openModal('settings');
    fillSettingsForm();
    paintProviderHints();
    paintSiteKey();
  };
  $('#btn-library').onclick = () => showLibrary();
  $('#btn-all').onclick = () => showLibrary();
  $('#lib-back').onclick = () => { if (state.doc) showViewer(); else showHome(); };
  $('#libsearch').oninput = () => renderLibrary(state.libCounts || {});
  wireLibraryFilters();
  $('#lib-open').onclick = () => { $('#fileinput').click(); };
  $('#link-key').onclick = () => $('#btn-settings').click();
  const nudgeBtn = $('#keynudge-btn');
  if (nudgeBtn) nudgeBtn.onclick = () => $('#btn-settings').click();
  $('#btn-theme').onclick = () => { state.settings.theme = state.settings.theme === 'dark' ? 'light' : 'dark'; applyTheme(); saveSettings(); };

  function fillSettingsForm() {
    $('#provider').value = state.settings.provider;
    $('#apikey').value = state.settings.key;
    $('#model').value = state.settings.model;
    $('#baseurl').value = state.settings.base_url;
    $('#level').value = state.settings.level;
    toggleCustom();
    paintProviderHints();
  }
  function toggleCustom() {
    const custom = $('#provider').value === 'custom';
    $('#baseurl').closest('.field').style.opacity = custom ? '1' : '.45';
    $('#baseurl').disabled = !custom;
  }

  function buildProviderOptions() {
    const sel = $('#provider');
    if (!state.providers.length || sel.dataset.built === '1') return;
    sel.innerHTML = state.providers.map((p) => {
      const tag = p.free ? ' — free' : '';
      const label = p.label.replace(/ — free tier| — free models/, '');
      return `<option value="${p.id}">${esc(label)}${tag}</option>`;
    }).join('');
    // same list for the site-key picker
    const siteOpts = state.providers
      .map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join('');
    $('#skprov').innerHTML = siteOpts;
    if ($('#skprov2')) {
      const freeOnes = state.providers.filter((p) => p.free && p.id !== 'custom');
      $('#skprov2').innerHTML = freeOnes
        .map((p) => `<option value="${p.id}">${esc(p.label)}</option>`).join('');
      const groq = freeOnes.find((p) => p.id === 'groq');
      if (groq) $('#skprov2').value = 'groq';
    }
    sel.dataset.built = '1';
  }
  $('#provider').onchange = () => { paintProviderHints(); toggleCustom(); detectLocalModels(); };

  /* ask the server what the local Ollama has installed (works when it runs on this machine) */
  async function detectLocalModels() {
    const list = $('#modellist');
    if (!list) return;
    if ($('#provider').value !== 'local') { list.innerHTML = ''; return; }
    try {
      const r = await fetch('/api/local-models');
      const d = await r.json();
      if (!d.ok) {
        $('#provnote').textContent =
          'Ollama did not answer on this computer. Start the Ollama app, then reopen this panel.';
        return;
      }
      list.innerHTML = (d.models || []).map((m) => `<option value="${esc(m)}"></option>`).join('');
      if (!$('#model').value.trim() && d.models && d.models.length) $('#model').value = d.models[0];
      $('#provnote').textContent =
        `Found ${d.count} model${d.count === 1 ? '' : 's'} installed (${d.gigabytes} GB): ` +
        (d.models || []).join(', ') + '. Pick one above — bigger is smarter, smaller is faster.';
    } catch (e) { /* offline is fine */ }
  }

  $('#savekey').onclick = () => {
    const prov = $('#provider').value;
    const typed = $('#apikey').value.trim();
    Object.assign(state.settings, {
      provider: prov, key: prov === 'local' ? (typed || 'local') : typed,
      model: $('#model').value.trim(), base_url: $('#baseurl').value.trim(), level: $('#level').value,
    });
    saveSettings(); closeModal('settings');
    paintKeyState();
    toast(state.settings.key ? 'Sage is connected.' : 'Running in offline mode.');
  };
  $('#clearkey').onclick = () => {
    state.settings.key = ''; $('#apikey').value = ''; saveSettings(); closeModal('settings');
    toast('Key removed — offline mode.');
  };
  const paintSBase = () => { $('#sbase-field').hidden = $('#skprov').value !== 'custom'; };
  const SK_MODELLESS = false;
  $('#skprov').onchange = paintSBase;
  paintSBase();
  $('#sk-save').onclick = saveSiteKey;
  $('#sk-clear').onclick = async () => {
    const st = $('#keystatus');
    try {
      const r = await fetch('/api/site-key/clear', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ passcode: $('#spass').value }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Could not remove the site key');
      state.ai = data;
      paintKeyState();
      toast('Site key removed', 2400);
      st.className = 'status ok show'; st.textContent = 'Removed — visitors now need their own key.';
    } catch (e) {
      st.className = 'status err show'; st.textContent = e.message;
    }
  };
  $('#sk-test').onclick = async () => {
    const st = $('#keystatus');
    st.className = 'status show'; st.textContent = 'Testing the site key…';
    try {
      const r = await fetch('/api/site-key/test', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || 'No site key');
      if (d.ok) { st.className = 'status ok show'; st.textContent = `Site key works — “${(d.reply || '').slice(0, 70)}”`; }
      else { st.className = 'status err show'; st.textContent = 'Key rejected: ' + (d.error || '').slice(0, 160); }
    } catch (e) { st.className = 'status err show'; st.textContent = e.message; }
  };

  $('#testkey').onclick = async () => {
    const st = $('#keystatus'), key = $('#apikey').value.trim();
    if (!key) { st.className = 'status err show'; st.textContent = 'Enter a key first.'; return; }
    st.className = 'status show'; st.textContent = 'Testing…';
    try {
      const r = await fetch('/api/verify-key', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ provider: $('#provider').value, api_key: key,
          model: $('#model').value.trim() || undefined, base_url: $('#baseurl').value.trim() || undefined }),
      });
      const d = await r.json();
      if (d.ok) { st.className = 'status ok show'; st.textContent = 'Key works — the model replied: “' + d.reply + '”'; }
      else { st.className = 'status err show'; st.textContent = 'Failed: ' + d.error; }
    } catch (e) { st.className = 'status err show'; st.textContent = 'Failed: ' + e.message; }
  };

  /* ------------------------------------------------------------------ reading pictures */
  /* Screenshots, photos and scans have no text layer. Ask the server to read them
     once, then the pages behave like any other text. */
  async function ocrPages(docId, limit = 2) {
    const needy = state.pages.filter((p) => !(p.text || '').trim() && p.image_url).slice(0, limit);
    if (!needy.length) return 0;
    let got = 0;
    for (const p of needy) {
      toast(`Reading the words in page ${p.n}…`, 2500);
      try {
        const r = await fetch(`/api/docs/${docId}/ocr/${p.n}`, { method: 'POST' });
        if (!r.ok) continue;
        const data = await r.json();
        if ((data.text || '').trim()) {
          p.text = data.text;
          p.image_only = false;
          got += 1;
        }
      } catch (e) { /* OCR is a bonus, never a blocker */ }
    }
    if (got) {
      const d = state.doc;
      if (d) {
        d.has_text = true;
        d.n_words = state.pages.reduce((a, p) => a + ((p.text || '').split(/\s+/).filter(Boolean).length), 0);
        $('#fmeta').textContent = `${d.n_pages} ${d.n_pages === 1 ? 'page' : 'pages'} · ${fmtNum(d.n_words)} words`;
        $('#find').disabled = false;
        $('#find').placeholder = 'Find in file  ⌘K';
      }
      toast(`Read ${got} page${got === 1 ? '' : 's'} from the picture`, 3000);
    }
    return got;
  }

  /* ------------------------------------------------------------------ library page */
  let libDocs = [];
  let libScope = 'all';          // 'all' = everyone, 'mine' = this device only
  let libMe = { label: '' };

  async function loadLibrary() {
    const grid = $('#libgrid');
    grid.innerHTML = '<p class="lib-none">Loading…</p>';
    try {
      const [{ docs, me }, counts] = await Promise.all([
        (await fetch('/api/docs', { cache: 'no-store' })).json(),
        (await fetch('/api/workspace', { cache: 'no-store' })).json(),
      ]);
      libDocs = docs || [];
      libMe = me || { label: '' };
      state.libCounts = counts.counts || {};
      renderLibrary(state.libCounts);
    } catch (e) {
      grid.innerHTML = '<p class="lib-none">Could not load your library.</p>';
    }
  }

  const libIco = (d) => (d.filename.endsWith('.pdf') ? 'pdf' : d.kind === 'sheet' ? 'sheet'
    : d.kind === 'slides' ? 'slide' : d.kind === 'image' ? 'img' : /\.(md|txt)$/i.test(d.filename) ? 'txt' : 'doc');

  const fmtWhen = (t) => {
    if (!t) return '';
    const s = Math.max(1, Date.now() / 1000 - t);
    const d = Math.floor(s / 86400);
    if (d > 6) return new Date(t * 1000).toLocaleDateString();
    if (d >= 1) return `${d}d ago`;
    const h = Math.floor(s / 3600);
    if (h >= 1) return `${h}h ago`;
    const m = Math.floor(s / 60);
    return m >= 1 ? `${m}m ago` : 'just now';
  };

  function renderLibrary(counts) {
    const grid = $('#libgrid');
    const q = ($('#libsearch').value || '').trim().toLowerCase();
    const mineCount = libDocs.filter((d) => d.mine).length;
    const base = libScope === 'mine' ? libDocs.filter((d) => d.mine) : libDocs;
    const rows = base.filter((d) => !q || d.filename.toLowerCase().includes(q));
    const total = base.length;
    $$('#libfilter .chip').forEach((c) => c.classList.toggle('on', c.dataset.scope === libScope));
    const dev = $('#libdevice');
    if (dev) dev.textContent = libMe.label ? `This device: ${libMe.label}` : '';
    if (dev) dev.title = 'Click to give this device a name (used to label the files you add)';
    $('#libcount').textContent = libScope === 'mine'
      ? `${mineCount} ${mineCount === 1 ? 'material' : 'materials'} from this device${q ? ` · ${rows.length} matching` : ''}`
      : `${libDocs.length} ${libDocs.length === 1 ? 'material' : 'materials'}${q ? ` · ${rows.length} matching` : ''}`;
    if (!total) {
      grid.innerHTML = libScope === 'mine'
        ? `<div class="lib-none"><b>Nothing added from this device yet</b>
             Switch to <b>Everyone</b> to see the other materials in the shared library.</div>`
        : `<div class="lib-none"><b>Your library is empty</b>
             Open a material from the home page and it will be kept here.</div>`;
      return;
    }
    if (!rows.length) { grid.innerHTML = '<div class="lib-none">Nothing matches that search.</div>'; return; }
    grid.innerHTML = rows.map((d) => {
      const n = counts[d.id] || 0;
      return `<div class="libcard" data-id="${d.id}">
        <span class="libcard-ico ${libIco(d)}">${esc((d.filename.split('.').pop() || '').slice(0, 4).toUpperCase())}</span>
        <div class="libcard-body">
          <b title="${esc(d.filename)}">${esc(d.filename)}</b>
          <span class="libcard-meta">${d.n_pages} ${d.n_pages === 1 ? 'page' : 'pages'} · ${fmtNum(d.n_words)} words · ${d.kind}</span>
          <span class="libcard-who">${d.mine ? '<em class="tag you">you</em>' : ''}added from <b>${esc(d.device || 'an earlier version')}</b>${d.created ? ` · ${fmtWhen(d.created)}` : ''}</span>
          <span class="libcard-tags">${n ? `<em class="tag">${n} in workspace</em>` : '<em class="tag quiet">workspace empty</em>'}</span>
        </div>
        <div class="libcard-acts">
          <button class="btn" data-lib-open="${d.id}">Open</button>
          <button class="btn" data-lib-add="${d.id}" title="Copy into a workspace">Add to workspace</button>
          <button class="lib-del" data-lib-del="${d.id}" title="Delete from the library">Delete</button>
        </div>
      </div>`;
    }).join('');
    grid.querySelectorAll('[data-lib-open]').forEach((b) => {
      b.onclick = () => { showViewer(); openById(b.dataset.libOpen); };
    });
    grid.querySelectorAll('[data-lib-add]').forEach((b) => {
      b.onclick = async () => {
        b.disabled = true;
        try {
          const r = await fetch('/api/workspace/from-library', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ doc_id: b.dataset.libAdd }),
          });
          if (!r.ok) throw new Error('Could not add that material');
          toast('Added to the workspace', 2000);
          loadLibrary();
        } catch (e) { toast(e.message, 3500); } finally { b.disabled = false; }
      };
    });
    grid.querySelectorAll('[data-lib-del]').forEach((b) => {
      b.onclick = async () => {
        const id = b.dataset.libDel;
        await fetch('/api/docs/' + id, { method: 'DELETE' });
        libDocs = libDocs.filter((d) => d.id !== id);
        if (state.doc && state.doc.id === id) { state.doc = null; }
        loadLibrary();
        loadRecent();
        toast('Removed from the library', 2000);
      };
    });
  }

  function showLibrary() {
    $('#home').hidden = true;
    $('#viewer').hidden = true;
    $('#libpage').hidden = false;
    document.body.classList.add('on-library');
    $('#libsearch').value = '';
    loadLibrary();
  }

  function wireLibraryFilters() {
    $$('#libfilter .chip').forEach((c) => {
      c.onclick = () => { libScope = c.dataset.scope; renderLibrary(state.libCounts || {}); };
    });
    const dev = $('#libdevice');
    if (dev) dev.onclick = () => {
      const now = localStorage.getItem('sm2.device') || '';
      const name = prompt('Name this device — your files will be labelled with it\n(e.g. "Aruna\'s phone", "Laptop")', now);
      if (name === null) return;
      const clean = name.trim().slice(0, 40);
      if (clean) localStorage.setItem('sm2.device', clean);
      else localStorage.removeItem('sm2.device');
      toast(clean ? `This device is now "${clean}"` : 'Device name cleared', 2500);
      loadLibrary();
    };
  }

  function showViewer() {
    $('#libpage').hidden = true;
    document.body.classList.remove('on-library');
    $('#viewer').hidden = false;
    $('#home').hidden = true;
  }

  /* pick a library material to drop into the open material's workspace */
  async function openPicker() {
    const list = $('#picklist');
    $('#pick-note').textContent = state.doc
      ? `Pick a material to copy into the workspace of ${state.doc.filename}.`
      : 'Pick a material to copy into the workspace.';
    list.innerHTML = '<p class="lib-none">Loading…</p>';
    openModal('libpicker');
    try {
      const [{ docs }, { files }] = await Promise.all([
        (await fetch('/api/docs', { cache: 'no-store' })).json(),
        (await fetch('/api/workspace' + (state.doc ? '?doc_id=' + encodeURIComponent(state.doc.id) : ''),
          { cache: 'no-store' })).json(),
      ]);
      const have = new Set((files || []).map((f) => f.doc_id).filter(Boolean));
      const rows = (docs || []).filter((d) => !have.has(d.id));
      if (!rows.length) {
        list.innerHTML = '<p class="lib-none">Everything in your library is already in this workspace.</p>';
        return;
      }
      list.innerHTML = rows.map((d) => `
        <div class="libitem">
          <div class="nm"><b>${esc(d.filename)}</b>
            <span>${d.n_pages} ${d.n_pages === 1 ? 'page' : 'pages'} · ${fmtNum(d.n_words)} words</span></div>
          <button class="btn" data-pick="${d.id}">Add</button>
        </div>`).join('');
      list.querySelectorAll('[data-pick]').forEach((b) => {
        b.onclick = async () => {
          b.disabled = true;
          try {
            const r = await fetch('/api/workspace/from-library', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ doc_id: b.dataset.pick, owner: state.doc ? state.doc.id : null }),
            });
            if (!r.ok) throw new Error('Could not add that material');
            closeModal('libpicker');
            await loadWorkspace();
            toast('Added to this workspace', 2200);
          } catch (e) { toast(e.message, 3500); b.disabled = false; }
        };
      });
    } catch (e) { list.innerHTML = '<p class="lib-none">Could not load your library.</p>'; }
  }

  async function loadRecent() {
    try {
      const { docs } = await (await fetch('/api/docs')).json();
      const box = $('#recent');
      if (!docs.length) { box.hidden = true; return; }
      box.hidden = false;
      $('#cards').innerHTML = docs.slice(0, 6).map((d) => {
        const cls = d.filename.endsWith('.pdf') ? 'pdf' : d.kind === 'sheet' ? 'sheet'
          : d.kind === 'slides' ? 'slide' : d.kind === 'image' ? 'img'
          : /\.(md|txt)$/i.test(d.filename) ? 'txt' : 'doc';
        const tag = (d.filename.split('.').pop() || '').slice(0, 4).toUpperCase();
        return `<button class="card" data-id="${d.id}">
          <span class="ico ${cls}">${esc(tag)}</span>
          <span class="nm"><b>${esc(d.filename)}</b><span>${d.n_pages} ${d.n_pages === 1 ? 'page' : 'pages'} · ${fmtNum(d.n_words)} words</span></span>
        </button>`;
      }).join('');
      $$('#cards .card').forEach((c) => { c.onclick = () => openById(c.dataset.id); });
    } catch (e) { /* ignore */ }
  }

  function showLoading(txt) { $('#loadtxt').textContent = txt || 'Working…'; $('#loading').classList.add('show'); }
  function hideLoading() { $('#loading').classList.remove('show'); }

  /* ------------------------------------------------------------------ keyboard */
  document.addEventListener('keydown', (e) => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
      e.preventDefault(); if (state.doc) $('#find').focus(); return;
    }
    if (e.key === 'Escape' && $('#libpage') && !$('#libpage').hidden) {
      e.preventDefault();
      if (state.doc) showViewer(); else showHome();
      return;
    }
    if (e.key === 'Escape') {
      if (state.regionMode) { setRegionMode(false); return; }
      hidePop(); bar.classList.remove('show');
      if ($$('.scrim.show').length) { $$('.scrim').forEach((s) => s.classList.remove('show')); return; }
      if (drawerOpen()) { setDrawer(false); return; }
      if (state.doc) showHome();
      return;
    }
    if (typing) return;
    if (e.key.toLowerCase() === 'a' && state.doc) { e.preventDefault(); setDrawer(!drawerOpen()); return; }
    if (e.key.toLowerCase() === 'r' && state.doc) { e.preventDefault(); $('#btn-region').click(); return; }
    if (e.key === '/' && state.doc) { e.preventDefault(); setDrawer(true); $('#ask').focus(); return; }
    if (e.key === 'PageDown' || e.key === 'j') { $('#stage').scrollBy({ top: window.innerHeight * 0.9, behavior: 'smooth' }); }
    if (e.key === 'PageUp' || e.key === 'k') { $('#stage').scrollBy({ top: -window.innerHeight * 0.9, behavior: 'smooth' }); }
  });

  /* never fail silently: a broken script or a stale bundle should say so */
  let warned = false;
  window.addEventListener('error', (e) => {
    if (warned) return;
    warned = true;
    toast('Something broke in the page (' + (e.message || 'script error') + '). Reloading the app usually fixes it.', 6000);
  });

  /* ------------------------------------------------------------------ boot */
  (async function versionGuard() {
    const meta = document.querySelector('meta[name="asset-version"]');
    if (!meta) return;
    try {
      const { asset_version } = await (await fetch('/api/version', { cache: 'no-store' })).json();
      if (asset_version && asset_version !== meta.content
          && !sessionStorage.getItem('sm.reloaded')) {
        sessionStorage.setItem('sm.reloaded', '1');
        location.reload();
      }
    } catch (e) { /* offline-ish; carry on */ }
  })();

  applyTheme();
  /* ------------------------------------------------------------------ offline hint */
  (function offlineWatch() {
    const say = () => toast('Offline — showing what is already saved on this device', 3600);
    window.addEventListener('offline', say);
    if (!navigator.onLine) setTimeout(say, 1200);
  })();

  /* ------------------------------------------------------------------ installable app (PWA) */
  (function initPWA() {
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js').catch(() => { /* http or blocked: ignore */ });
      });
    }
    const btn = $('#btn-install');
    if (!btn) return;
    const standalone = () => window.matchMedia('(display-mode: standalone)').matches
      || window.navigator.standalone === true;
    if (standalone()) return;

    let deferred = null;
    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault();
      deferred = e;
      btn.hidden = false;
    });
    window.addEventListener('appinstalled', () => {
      btn.hidden = true;
      toast('StudyMate is installed', 2400);
    });
    btn.onclick = async () => {
      if (deferred) {
        deferred.prompt();
        const choice = await deferred.userChoice.catch(() => null);
        deferred = null;
        btn.hidden = true;
        if (choice && choice.outcome === 'accepted') toast('Installing…', 2000);
        return;
      }
      // iOS/Safari has no prompt — explain how to add it by hand
      toast(/iphone|ipad|ipod/i.test(navigator.userAgent)
        ? 'Safari: Share → Add to Home Screen'
        : 'Use your browser menu → Install app', 4200);
    };
    // iOS never fires beforeinstallprompt, so offer the hint there
    if (/iphone|ipad|ipod/i.test(navigator.userAgent) && !standalone()) btn.hidden = false;
  })();

  loadProviders().then(() => { buildProviderOptions(); paintKeyState(); });
  loadAIStatus();

  initGrip();
  setTab('chat');
  fillSettingsForm();
  paintKeyState();
  showHome();
  loadWorkspace();
})();
