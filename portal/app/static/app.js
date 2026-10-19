// Small progressive enhancements for the portal: theme, shortcuts, copy, menus, client-side filters, selection.
(function () {
  var app = document.getElementById('app');
  var root = document.documentElement;

  // ---- theme ------------------------------------------------------------
  function setTheme(dark, save) {
    app.classList.toggle('dark', dark);
    root.style.background = dark ? '#0d1117' : '';
    if (save) { try { localStorage.setItem('mmrag-theme', dark ? 'dark' : 'light'); } catch (e) {} }
  }
  setTheme(root.classList.contains('dark-pending'), false);
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-theme-toggle]')) setTheme(!app.classList.contains('dark'), true);
  });

  // ---- "/" focuses search; Ctrl/Cmd+Enter submits the closest form ------
  document.addEventListener('keydown', function (e) {
    var t = e.target, typing = /INPUT|TEXTAREA|SELECT/.test(t.tagName) || t.isContentEditable;
    if (e.key === '/' && !typing) { var s = document.getElementById('gsearch'); if (s) { e.preventDefault(); s.focus(); } }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && t.form && t.tagName === 'TEXTAREA') { e.preventDefault(); t.form.requestSubmit(); }
  });

  // ---- copy buttons: data-copy="text" or data-copy-from="#id" -------------
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-copy],[data-copy-from]');
    if (!b) return;
    var text = b.getAttribute('data-copy');
    if (text == null) { var el = document.querySelector(b.getAttribute('data-copy-from')); text = el ? (el.value != null && el.tagName === 'INPUT' ? el.value : el.innerText) : ''; }
    var done = function () { var o = b.getAttribute('aria-label') || ''; b.setAttribute('aria-label', 'Copied'); b.classList.add('copied'); b.title = 'Copied'; setTimeout(function () { b.setAttribute('aria-label', o); b.title = ''; b.classList.remove('copied'); }, 1200); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(done);
    else { var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); } catch (x) {} ta.remove(); done(); }
  });

  // ---- close <details class="menu"> on outside click / Escape -------------
  document.addEventListener('click', function (e) {
    document.querySelectorAll('details.menu[open]').forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute('open'); });
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') document.querySelectorAll('details.menu[open]').forEach(function (d) { d.removeAttribute('open'); });
  });

  // ---- confirm: data-confirm="message" on forms ---------------------------
  document.addEventListener('submit', function (e) {
    var f = e.target, msg = f.getAttribute('data-confirm');
    var sub = e.submitter && e.submitter.getAttribute('data-confirm');
    if ((sub || msg) && !window.confirm(sub || msg)) e.preventDefault();
  }, true);

  // ---- client-side list filter: input[data-filter="#list"] + [data-f] rows --
  function applyFilters(list) {
    var rows = list.querySelectorAll('[data-f]'), shown = 0;
    var inputs = document.querySelectorAll('[data-filter="#' + list.id + '"]');
    rows.forEach(function (r) {
      var ok = true;
      inputs.forEach(function (i) {
        var key = i.getAttribute('data-key'), val = (i.value || '').toLowerCase().trim();
        if (!val || val === 'all') return;
        if (key) ok = ok && (r.getAttribute('data-' + key) || '').toLowerCase() === val;
        else ok = ok && r.getAttribute('data-f').toLowerCase().indexOf(val) !== -1;
      });
      r.hidden = !ok; if (ok) shown++;
    });
    var cnt = document.querySelector('[data-count-for="#' + list.id + '"]');
    if (cnt) cnt.textContent = shown + ' ' + cnt.getAttribute('data-noun') + (shown === 1 ? '' : 's');
    var empty = list.querySelector('[data-empty]'); if (empty) empty.hidden = shown !== 0;
  }
  document.addEventListener('input', function (e) {
    var sel = e.target.getAttribute && e.target.getAttribute('data-filter');
    if (sel) { var l = document.querySelector(sel); if (l) applyFilters(l); }
  });
  document.addEventListener('change', function (e) {
    var sel = e.target.getAttribute && e.target.getAttribute('data-filter');
    if (sel) { var l = document.querySelector(sel); if (l) applyFilters(l); }
  });

  // ---- segmented views (source detail): [data-view-btn] / [data-view] -------
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-view-btn]');
    if (!b) return;
    var name = b.getAttribute('data-view-btn');
    document.querySelectorAll('[data-view-btn]').forEach(function (x) { x.classList.toggle('on', x === b); x.setAttribute('aria-selected', x === b); });
    document.querySelectorAll('[data-view]').forEach(function (p) { p.hidden = p.getAttribute('data-view') !== name; });
    try { history.replaceState(null, '', '?view=' + name); } catch (x) {}
  });

  // ---- document selection (documents page) ----------------------------------
  function selUpdate() {
    var boxes = Array.prototype.slice.call(document.querySelectorAll('input[data-sel]'));
    var visible = boxes.filter(function (b) { return !b.closest('tr').hidden; });
    var n = boxes.filter(function (b) { return b.checked; }).length;
    boxes.forEach(function (b) { b.closest('tr').classList.toggle('sel', b.checked); });
    var all = document.querySelector('input[data-sel-all]');
    if (all) { all.checked = visible.length > 0 && visible.every(function (b) { return b.checked; }); all.indeterminate = n > 0 && !all.checked; }
    document.querySelectorAll('[data-sel-none]').forEach(function (x) { x.hidden = n > 0; });
    document.querySelectorAll('[data-sel-some]').forEach(function (x) { x.hidden = n === 0; });
    document.querySelectorAll('[data-sel-count]').forEach(function (x) { x.textContent = n + ' selected'; });
  }
  document.addEventListener('change', function (e) {
    if (e.target.matches('input[data-sel-all]')) {
      document.querySelectorAll('input[data-sel]').forEach(function (b) { if (!b.closest('tr').hidden) b.checked = e.target.checked; });
    }
    if (e.target.matches('input[data-sel], input[data-sel-all]')) selUpdate();
  });
  document.addEventListener('click', function (e) {
    if (e.target.closest('[data-sel-clear]')) { document.querySelectorAll('input[data-sel]').forEach(function (b) { b.checked = false; }); selUpdate(); }
  });
  if (document.querySelector('input[data-sel]')) selUpdate();

  // ---- type-to-confirm: input[data-confirm-value] enables [data-confirm-target] --
  document.addEventListener('input', function (e) {
    var want = e.target.getAttribute && e.target.getAttribute('data-confirm-value');
    if (want == null) return;
    var btn = document.querySelector(e.target.getAttribute('data-confirm-target'));
    var ok = e.target.value.trim() === want;
    btn.disabled = !ok; btn.classList.toggle('dngf', ok); btn.classList.toggle('dng', !ok);
  });

  // ---- log tools (run page): search + errors-only ----------------------------
  function logFilter() {
    var q = (document.getElementById('logq') || {}).value || '', errs = (document.getElementById('logerr') || {}).checked;
    document.querySelectorAll('#log .ln').forEach(function (l) {
      var ok = (!errs || l.classList.contains('err')) && (!q || l.textContent.toLowerCase().indexOf(q.toLowerCase()) !== -1);
      l.hidden = !ok;
    });
  }
  document.addEventListener('input', function (e) { if (e.target.id === 'logq') logFilter(); });
  document.addEventListener('change', function (e) { if (e.target.id === 'logerr') logFilter(); });
  document.body.addEventListener('htmx:afterSwap', function () { logFilter(); if (document.querySelector('input[data-sel]')) selUpdate(); });

  // ---- ask: retrieval mode buttons -------------------------------------------
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-mode]');
    if (!b) return;
    var m = b.getAttribute('data-mode');
    var inp = document.getElementById('mode'); if (inp) inp.value = m;
    document.querySelectorAll('[data-mode]').forEach(function (x) {
      var on = x.getAttribute('data-mode') === m;
      if (x.closest('.modes')) x.classList.toggle('on', on); else x.style.background = on ? 'var(--acc-bg)' : 'transparent';
      x.setAttribute('aria-checked', on);
    });
    var d = document.getElementById('modeDesc'); if (d) d.textContent = b.getAttribute('data-desc');
    var n = document.getElementById('modeName'); if (n) n.textContent = m;
  });
  var askForm = document.getElementById('askForm');
  if (askForm) askForm.addEventListener('submit', function () { var b = document.getElementById('askBtn'); if (b) { b.disabled = true; b.lastChild.textContent = 'Asking…'; } });

  // ---- upload: drag & drop + queue preview ------------------------------------
  var drop = document.querySelector('.drop'), finput = document.getElementById('files');
  if (drop && finput) {
    var existing = {}; try { existing = JSON.parse(drop.getAttribute('data-existing') || '{}'); } catch (x) {}
    var dt = new DataTransfer();
    var render = function () {
      finput.files = dt.files;
      var q = document.getElementById('queue'), list = document.getElementById('queueList');
      q.hidden = dt.files.length === 0;
      document.getElementById('queueCount').textContent = dt.files.length;
      list.innerHTML = '';
      Array.prototype.forEach.call(dt.files, function (f, i) {
        var rep = existing.hasOwnProperty(f.name);
        var row = document.createElement('div');
        row.className = 'br'; if (rep) row.style.background = 'var(--warn-bg)';
        row.innerHTML = (rep ? document.getElementById('tplWarn').innerHTML : document.getElementById('tplFile').innerHTML) +
          '<div style="flex:1 1 240px;min-width:0"><div style="font-weight:600" data-q-name></div><span class="mut" style="font-size:12px" data-q-note></span></div>' +
          '<span class="lbl ' + (rep ? 'warn">Replaces existing' : 'acc">New') + '</span>' +
          '<button type="button" class="btn sm" aria-label="Remove from queue" style="background:transparent;border-color:transparent">' + document.getElementById('tplX').innerHTML + '</button>';
        row.querySelector('[data-q-name]').textContent = f.name;
        row.querySelector('[data-q-note]').textContent = rep ? 'Replaces the version uploaded ' + existing[f.name] + '. Its old chunks are removed from LightRAG.' : 'New document · ' + (f.size > 1048576 ? (f.size / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(f.size / 1024)) + ' KB');
        row.querySelector('button[aria-label="Remove from queue"]').addEventListener('click', function () {
          var n = new DataTransfer(); Array.prototype.forEach.call(dt.files, function (g, j) { if (j !== i) n.items.add(g); }); dt = n; render();
        });
        list.appendChild(row);
      });
    };
    var add = function (files) { Array.prototype.forEach.call(files, function (f) {
      var dup = Array.prototype.some.call(dt.files, function (g) { return g.name === f.name; }); if (!dup) dt.items.add(f); }); render(); };
    finput.addEventListener('change', function () { var fs = Array.prototype.slice.call(finput.files); add(fs); });
    ['dragenter', 'dragover'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add('over'); }); });
    ['dragleave', 'drop'].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove('over'); }); });
    drop.addEventListener('drop', function (e) { add(e.dataTransfer.files); });
    var clr = document.getElementById('clearQueue');
    if (clr) clr.addEventListener('click', function () { dt = new DataTransfer(); render(); });
    var auto = document.getElementById('autoIngest'), ing = document.getElementById('ingest'), go = document.getElementById('uploadGo');
    var sync = function () { ing.value = auto.checked ? '1' : '0'; go.lastChild.textContent = auto.checked ? 'Upload and ingest' : 'Upload'; };
    if (auto) { auto.addEventListener('change', sync); sync(); }
    var only = document.getElementById('uploadOnly');
    if (only) only.addEventListener('click', function () { ing.value = '0'; });
    if (go) go.addEventListener('click', function () { ing.value = auto && !auto.checked ? '0' : '1'; });
  }
})();

// ---- dismissible notices remembered per browser ------------------------------
(function () {
  var seen = {}; try { seen = JSON.parse(localStorage.getItem('mmrag-dismissed') || '{}'); } catch (e) {}
  document.querySelectorAll('[data-dismiss-key]').forEach(function (el) { if (!seen[el.getAttribute('data-dismiss-key')]) el.hidden = false; });
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-dismiss]'); if (!b) return;
    var box = b.closest('[data-dismiss-key]'); if (!box) return;
    seen[box.getAttribute('data-dismiss-key')] = 1; box.remove();
    try { localStorage.setItem('mmrag-dismissed', JSON.stringify(seen)); } catch (x) {}
  });
})();
