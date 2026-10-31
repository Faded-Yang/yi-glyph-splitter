/* app.js —— 彝文切字工具 前端主逻辑（原生 JS，无框架、无 CDN、离线可用） */
(function () {
  'use strict';
  var API = window.YI.api;

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function num(v, d) { var n = parseFloat(v); return isNaN(n) ? d : n; }
  function esc(s) {
    s = String(s == null ? '' : s);
    return s.split('&').join('&amp;').split('<').join('&lt;').split('>').join('&gt;').split('"').join('&quot;');
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }

  // ------------------------------------------------------------------ 状态
  var S = {
    project: null, pages: [], settings: { ai: {}, defaults: {} },
    pageId: null, page: null, pageStatus: '', boxes: [], selId: null,
    hist: [], redo: [], lastSnap: '',
    dirty: false, saveState: 'idle', saveTimer: null, saving: false,
    jobs: {}, jobOrder: [], jobProgress: {}, pollTimer: null, activeJobId: null,
    proposals: [], aiAvailable: true, aiConfigured: false,
    previewW: 1600, previewToken: 1, previewRebuildTried: -1, detailTimer: null, detailRect: null,
    checked: {}, filter: '', armed: false, modalOpen: null,
    tempId: 1, lastExportOut: ''
  };
  var view = null;

  function pageById(pid) {
    for (var i = 0; i < S.pages.length; i++) { if (S.pages[i].id === pid) { return S.pages[i]; } }
    return null;
  }
  function selBox() {
    if (S.selId == null) { return null; }
    for (var i = 0; i < S.boxes.length; i++) { if (S.boxes[i].id === S.selId) { return S.boxes[i]; } }
    return null;
  }
  function statusInfo(p) {
    var raw = String(p && p.status != null ? p.status : '').toLowerCase();
    var map = {
      '': 'pending', 'pending': 'pending', 'new': 'pending', 'imported': 'pending', 'idle': 'pending',
      'processing': 'processing', 'running': 'processing',
      'processed': 'processed', 'ready': 'processed', 'segmented': 'processed',
      'reviewing': 'reviewing', 'review': 'reviewing', 'editing': 'reviewing',
      'done': 'done', 'complete': 'done', 'completed': 'done', 'exported': 'done', 'finished': 'done',
      'error': 'error'
    };
    var k = map[raw] || 'pending';
    var labels = { pending: '未处理', processing: '处理中', processed: '已处理', reviewing: '复核中', done: '已完成', error: '出错' };
    var colors = { pending: '#64748b', processing: '#3b82f6', processed: '#38bdf8', reviewing: '#f59e0b', done: '#22c55e', error: '#ef4444' };
    var label = p && p.status_label ? String(p.status_label) : labels[k];
    return { key: k, label: label, color: colors[k] };
  }

  // ------------------------------------------------------------------ 提示
  function toast(msg, type, detail) {
    var wrap = $('#toastWrap');
    var t = el('div', 'toast ' + (type || ''));
    t.appendChild(el('div', 'toast-msg', msg));
    if (detail) { t.appendChild(el('div', 'toast-detail', detail)); }
    wrap.appendChild(t);
    var ttl = type === 'err' ? 8000 : 2800;
    setTimeout(function () {
      if (!t.parentNode) { return; }
      t.classList.add('out');
      setTimeout(function () { if (t.parentNode) { t.parentNode.removeChild(t); } }, 220);
    }, ttl);
    while (wrap.children.length > 5) { wrap.removeChild(wrap.firstChild); }
  }

  function setSaveState(st) {
    S.saveState = st;
    var chip = $('#saveChip');
    var map = { idle: ['未保存', ''], dirty: ['未保存', 'dirty'], saving: ['保存中…', 'saving'], saved: ['已保存', 'saved'], error: ['保存失败', 'error'] };
    var m = map[st] || map.idle;
    chip.textContent = m[0];
    chip.className = 'save-chip ' + m[1];
  }

  function setStageHint(text) {
    var h = $('#stageHint');
    if (!text) { h.classList.add('hidden'); return; }
    h.textContent = text;
    h.classList.remove('hidden');
  }

  // ------------------------------------------------------------------ 页面列表
  function renderPages() {
    var list = $('#pageList');
    var scroll = list.scrollTop;
    list.textContent = '';
    $('#pageCount').textContent = S.pages.length + ' 页';
    var rows = S.pages.filter(function (p) { return !S.filter || statusInfo(p).key === S.filter; });
    if (!rows.length) {
      list.appendChild(el('li', 'page-empty', S.pages.length ? '没有符合筛选的页面' : '还没有页面：点上方「导入 TIF」或「导入文件夹」'));
    }
    rows.forEach(function (p) {
      var st = statusInfo(p);
      var li = el('li', 'page-row' + (p.id === S.pageId ? ' active' : ''));
      li.dataset.pid = p.id;
      var chk = document.createElement('input');
      chk.type = 'checkbox'; chk.className = 'row-chk'; chk.checked = !!S.checked[p.id];
      chk.title = '勾选后可批量处理 / 导出';
      chk.addEventListener('click', function (e) { e.stopPropagation(); });
      chk.addEventListener('change', function () { S.checked[p.id] = chk.checked; renderSelCount(); });
      li.appendChild(chk);
      var dot = el('i', 'dot');
      dot.style.background = st.color;
      dot.title = st.label;
      li.appendChild(dot);
      var mid = el('div', 'p-mid');
      var nm = el('div', 'p-name', p.name || p.id);
      nm.title = (p.src || p.name || '') + '\n状态：' + st.label + (p.error ? ('\n错误：' + p.error) : '');
      mid.appendChild(nm);
      var nbox = p.n_boxes != null ? p.n_boxes : 0;
      var nrev = p.reviewed != null ? p.reviewed : 0;
      var sub = el('div', 'p-sub');
      if (st.key === 'error' && p.error) {
        sub.textContent = p.error;
        sub.classList.add('err');
      } else {
        sub.textContent = nbox + ' 框 · 已复核 ' + nrev + (nbox ? (' · ' + Math.round(nrev / nbox * 100) + '%') : '');
      }
      mid.appendChild(sub);
      li.appendChild(mid);
      var badge = el('span', 'p-badge', st.label);
      badge.style.color = st.color;
      li.appendChild(badge);
      var prog = el('div', 'p-prog');
      var fill = el('i');
      var jp = S.jobProgress[p.id];
      if (jp) {
        fill.style.width = Math.round(clamp(jp.frac || 0, 0, 1) * 100) + '%';
        fill.className = 'job';
      } else {
        fill.style.width = (nbox ? Math.round(nrev / nbox * 100) : 0) + '%';
      }
      prog.appendChild(fill);
      li.appendChild(prog);
      li.addEventListener('click', function () { selectPage(p.id); });
      list.appendChild(li);
    });
    list.scrollTop = scroll;
    renderSelCount();
  }

  function renderSelCount() {
    var ids = Object.keys(S.checked).filter(function (k) { return S.checked[k]; });
    var btn = $('#btnRemove');
    btn.disabled = !ids.length;
    btn.textContent = ids.length ? ('移除选中 ' + ids.length + ' 页') : '移除选中';
    $('#selPagesTip').textContent = ids.length ? ('已选 ' + ids.length + ' 页') : '';
    var all = $('#chkAll');
    var visible = S.pages.filter(function (p) { return !S.filter || statusInfo(p).key === S.filter; });
    all.checked = visible.length > 0 && visible.every(function (p) { return S.checked[p.id]; });
    all.indeterminate = !all.checked && visible.some(function (p) { return S.checked[p.id]; });
  }

  // ------------------------------------------------------------------ 载入
  async function loadState() {
    try {
      var st = await API.get('/api/state');
      applyState(st);
    } catch (e) {
      toast('连接后端失败', 'err', e.message);
      setStageHint('无法连接后端服务（' + e.message + '）');
    }
  }

  function applyState(st) {
    if (!st) { return; }
    S.settings = st.settings || S.settings;
    S.pages = Array.isArray(st.pages) ? st.pages : [];
    var pj = st.project || {};
    if (typeof pj === 'string') { pj = { path: pj }; }
    S.project = pj;
    var path = pj.path || st.project_path || '';
    var name = pj.name || st.project_name || path;
    var label = $('#projectLabel');
    label.textContent = name ? ('项目：' + name) : '未打开项目';
    label.title = path || '未打开项目';
    renderPages();
    if (!S.pages.length) { clearPage(); return; }
    if (!S.pageId || !pageById(S.pageId)) {
      selectPage(S.pages[0].id);
    } else {
      renderPageInfo();
    }
  }

  function clearPage() {
    S.pageId = null; S.page = null; S.boxes = []; S.selId = null; S.proposals = [];
    view.setData({ w: 0, h: 0, boxes: [], selId: null, columns: [] });
    view.setImage('');
    renderPages(); renderInspector(); renderAI(); renderPageInfo(); renderHint();
    setStageHint('还没有页面：先导入 TIF 或文件夹');
  }

  async function selectPage(pid) {
    if (!pid) { return; }
    if (S.pageId === pid && S.page) { return; }
    if (S.dirty) { await flushSave(); }
    S.pageId = pid; S.selId = null; S.armed = false; S.dirty = false; S.proposals = [];
    setSaveState('saved');
    view.setArmed(false);
    var res;
    try {
      res = await API.get('/api/pages/' + encodeURIComponent(pid));
    } catch (e) {
      toast('载入页面失败', 'err', e.message);
      S.pageId = null;
      setStageHint('载入页面失败：' + e.message);
      return;
    }
    if (S.pageId !== pid) { return; }
    var pg = res.page || {};
    S.page = {
      w: pg.w || pg.width || 0, h: pg.h || pg.height || 0,
      columns: pg.columns || [], source: pg.source || ''
    };
    S.pageStatus = res.status || '';
    S.aiAvailable = res.ai_available !== false;
    S.boxes = (res.boxes || []).map(normBox);
    S.tempId = S.boxes.reduce(function (m, b) { return Math.max(m, b.id || 0); }, 0) + 1;
    var sum = pageById(pid);
    if (sum) {
      sum.n_boxes = S.boxes.length;
      sum.reviewed = S.boxes.filter(function (b) { return !!b.review; }).length;
      if (res.status) { sum.status = res.status; }
      if (res.status_label) { sum.status_label = res.status_label; }
      if (S.page.w) { sum.width = S.page.w; sum.height = S.page.h; }
    }
    S.previewW = 1600;
    view.setData({ w: S.page.w, h: S.page.h, boxes: S.boxes, selId: null, columns: S.page.columns });
    setStageHint(S.boxes.length ? '' : '该页还没有框：先「处理本页」，或按 A 在画布上手动框选');
    loadPreview();
    renderPages(); renderPageInfo(); renderInspector(); renderAI(); renderHint();
    loadProposals(pid);
  }

  function normBox(b) {
    return {
      id: b.id != null ? b.id : 0,
      x0: Math.round(num(b.x0, 0)), y0: Math.round(num(b.y0, 0)),
      x1: Math.round(num(b.x1, 0)), y1: Math.round(num(b.y1, 0)),
      flags: Array.isArray(b.flags) ? b.flags.slice() : [],
      review: b.review ? (b.review | 0) : 0,
      note: b.note || ''
    };
  }

  function previewUrl(pid, w, rebuild) {
    return '/api/pages/' + encodeURIComponent(pid) + '/preview.png?w=' + w +
      '&boxes=0&t=' + S.previewToken + (rebuild ? '&rebuild=1' : '');
  }

  // A preview can be missing / half-written (cancelled or crashed processing).
  // First failure -> ask the server to rebuild it once; second -> tell the user.
  function retryPreviewWithRebuild(url) {
    if (!S.pageId || S.previewRebuildTried === S.previewToken) { return false; }
    S.previewRebuildTried = S.previewToken;
    var w = S.previewW || 1600;
    setStageHint('预览图缺失或损坏，正在重建…');
    view.setImage(previewUrl(S.pageId, w, true));
    return true;
  }

  function loadPreview() {
    if (!S.pageId || !S.page) { return; }
    var w = Math.max(900, Math.min(2000, Math.round(S.page.w || 1600)));
    S.previewW = w;
    view.setImage(previewUrl(S.pageId, w));
  }

  async function reloadCurrentPage() {
    if (!S.pageId) { return; }
    var pid = S.pageId;
    if (S.dirty) { await flushSave(); }
    S.previewToken += 1;
    var res;
    try { res = await API.get('/api/pages/' + encodeURIComponent(pid)); } catch (e) { toast('重新载入失败', 'err', e.message); return; }
    if (S.pageId !== pid) { return; }
    S.boxes = (res.boxes || []).map(normBox);
    S.pageStatus = res.status || '';
    S.aiAvailable = res.ai_available !== false;
    var sum = pageById(pid);
    if (sum) {
      sum.n_boxes = S.boxes.length;
      sum.reviewed = S.boxes.filter(function (b) { return !!b.review; }).length;
      if (res.status) { sum.status = res.status; }
      if (res.status_label) { sum.status_label = res.status_label; }
    }
    S.selId = null; S.dirty = false; setSaveState('saved');
    view.setData({ w: S.page.w, h: S.page.h, boxes: S.boxes, selId: null, columns: S.page.columns });
    loadPreview();
    renderInspector(); renderAI(); renderHint(); renderPages(); renderPageInfo();
    toast('已重新载入本页', 'ok');
  }

  // ------------------------------------------------------------------ 保存
  function scheduleSave() {
    if (S.saveTimer) { clearTimeout(S.saveTimer); }
    S.dirty = true;
    setSaveState('dirty');
    S.saveTimer = setTimeout(function () { S.saveTimer = null; doSave(); }, 400);
  }

  function flushSave() {
    if (S.saveTimer) { clearTimeout(S.saveTimer); S.saveTimer = null; }
    return doSave();
  }

  async function doSave() {
    if (!S.pageId || !S.dirty || S.saving) { return; }
    var pid = S.pageId;
    var selIdx = -1;
    for (var i = 0; i < S.boxes.length; i++) { if (S.boxes[i].id === S.selId) { selIdx = i; } }
    var payload = S.boxes.map(function (b) {
      return {
        x0: b.x0, y0: b.y0, x1: b.x1, y1: b.y1,
        flags: b.flags || [], review: b.review || 0, note: b.note || ''
      };
    });
    S.saving = true;
    setSaveState('saving');
    var res = null;
    try {
      res = await API.put('/api/pages/' + encodeURIComponent(pid) + '/boxes', { boxes: payload });
    } catch (e) {
      S.saving = false;
      if (S.pageId === pid) { setSaveState('error'); }
      toast('保存失败', 'err', e.message);
      return;
    }
    S.saving = false;
    if (S.pageId !== pid) { return; }
    var rb = (res && res.boxes) || [];
    if (rb.length) {
      S.boxes = rb.map(normBox);
      S.selId = (selIdx >= 0 && selIdx < S.boxes.length) ? S.boxes[selIdx].id : null;
    }
    S.dirty = false;
    setSaveState('saved');
    view.setData({ w: S.page.w, h: S.page.h, boxes: S.boxes, selId: S.selId, columns: S.page.columns });
    var p = pageById(pid);
    if (p) {
      if (res && res.n_boxes != null) { p.n_boxes = res.n_boxes; }
      if (res && res.reviewed != null) { p.reviewed = res.reviewed; }
      if (res && res.status) { p.status = res.status; }
      if (res && res.status_label) { p.status_label = res.status_label; }
    }
    renderPages(); renderInspector(); renderHint(); renderPageInfo();
    toast('已保存 ' + S.boxes.length + ' 框', 'ok');
    if (res && res.warnings && res.warnings.length) { toast('保存提示', 'warn', res.warnings.join('\n')); }
  }

  // ------------------------------------------------------------------ 撤销
  function snapshot() {
    return JSON.stringify(S.boxes.map(function (b) {
      return [b.id, b.x0, b.y0, b.x1, b.y1, b.review, b.flags, b.note];
    }));
  }
  function pushUndo() {
    S.hist.push({ boxes: JSON.stringify(S.boxes), selId: S.selId });
    if (S.hist.length > 50) { S.hist.shift(); }
    S.redo.length = 0;
  }
  function restore(snap) {
    S.boxes = JSON.parse(snap.boxes).map(normBox);
    S.selId = snap.selId;
    if (S.selId != null && !S.boxes.some(function (b) { return b.id === S.selId; })) { S.selId = null; }
    view.setData({ w: S.page.w, h: S.page.h, boxes: S.boxes, selId: S.selId, columns: S.page.columns });
    setSaveState('dirty');
    renderInspector(); renderHint(); renderAI();
    scheduleSave();
  }
  function undo() {
    if (!S.hist.length) { toast('没有可撤销的操作', 'warn'); return; }
    S.redo.push({ boxes: JSON.stringify(S.boxes), selId: S.selId });
    restore(S.hist.pop());
    toast('已撤销', 'ok');
  }
  function redo() {
    if (!S.redo.length) { toast('没有可重做的操作', 'warn'); return; }
    S.hist.push({ boxes: JSON.stringify(S.boxes), selId: S.selId });
    if (S.hist.length > 50) { S.hist.shift(); }
    restore(S.redo.pop());
    toast('已重做', 'ok');
  }

  function mutate(fn) {
    if (!S.pageId) { return; }
    pushUndo();
    fn();
    setSaveState('dirty');
    view.setData({ w: S.page.w, h: S.page.h, boxes: S.boxes, selId: S.selId, columns: S.page.columns });
    renderInspector(); renderHint(); renderPageInfo(); renderAI();
    scheduleSave();
  }

  // ------------------------------------------------------------------ 框排序
  function colOf(b, cols) {
    var cx = (b.x0 + b.x1) / 2;
    for (var i = 0; i < cols.length; i++) {
      if (cx >= cols[i].x0 && cx < cols[i].x1) { return i; }
    }
    return cols.length;
  }
  function readingOrder() {
    var cols = (S.page && S.page.columns) || [];
    var arr = S.boxes.slice();
    if (cols.length) {
      arr.sort(function (a, b) {
        var ca = colOf(a, cols), cb = colOf(b, cols);
        if (ca !== cb) { return ca - cb; }
        if (Math.abs(a.y0 - b.y0) > 4) { return a.y0 - b.y0; }
        return a.x0 - b.x0;
      });
    } else {
      arr.sort(function (a, b) {
        var ka = Math.round((a.x0 + a.x1) / 160), kb = Math.round((b.x0 + b.x1) / 160);
        if (ka !== kb) { return ka - kb; }
        return a.y0 - b.y0;
      });
    }
    return arr;
  }

  // ------------------------------------------------------------------ 框操作
  function setSel(id) {
    S.selId = id;
    view.setSelId(id);
    if (id != null) { var b = selBox(); if (b) { view.ensureVisible(b); } }
    renderInspector(); renderHint();
  }
  function stepSel(dir) {
    var ord = readingOrder();
    if (!ord.length) { return; }
    var cur = selBox();
    var i = cur ? ord.indexOf(cur) : -1;
    var n = i < 0 ? ord[0] : ord[clamp(i + dir, 0, ord.length - 1)];
    setSel(n.id);
  }
  function setReview(v) {
    var b = selBox();
    if (!b) { toast('先选中一个框', 'warn'); return; }
    if (b.review === v) { return; }
    mutate(function () { b.review = v; });
    toast(v === 1 ? '已确认' : (v === 2 ? '已标记存疑' : '已复位'), v === 2 ? 'warn' : 'ok');
  }
  function deleteSel() {
    var b = selBox();
    if (!b) { toast('先选中一个框', 'warn'); return; }
    var ord = readingOrder();
    var idx = ord.indexOf(b);
    mutate(function () {
      S.boxes = S.boxes.filter(function (x) { return x !== b; });
      S.selId = null;
    });
    var after = readingOrder();
    var next = after[Math.min(idx, after.length - 1)] || null;
    if (next) { setSel(next.id); }
    toast('已删除 1 框', 'ok');
  }
  function splitSel() {
    var b = selBox();
    if (!b) { toast('先选中一个框', 'warn'); return; }
    var m = view.getMouseImg();
    var y = m ? Math.round(m.y) : Math.round((b.y0 + b.y1) / 2);
    if (y <= b.y0 + 8 || y >= b.y1 - 8) {
      y = Math.round((b.y0 + b.y1) / 2);
      toast('鼠标不在框内：已按框中线拆分', 'warn');
    }
    var nb = { id: S.tempId, x0: b.x0, y0: y, x1: b.x1, y1: b.y1, flags: [], review: 0, note: '' };
    S.tempId += 1;
    mutate(function () {
      b.y1 = y;
      S.boxes.push(nb);
      S.selId = nb.id;
    });
    toast('已拆分', 'ok');
  }
  function mergeSel(dir) {
    var b = selBox();
    if (!b) { toast('先选中一个框', 'warn'); return; }
    var ord = readingOrder();
    var i = ord.indexOf(b);
    var other = ord[i + dir];
    if (!other) { toast(dir > 0 ? '下面没有可合并的框' : '上面没有可合并的框', 'warn'); return; }
    mutate(function () {
      b.x0 = Math.min(b.x0, other.x0); b.y0 = Math.min(b.y0, other.y0);
      b.x1 = Math.max(b.x1, other.x1); b.y1 = Math.max(b.y1, other.y1);
      if (b.review !== other.review) { b.review = 0; }
      b.note = [b.note, other.note].filter(Boolean).join(' / ');
      S.boxes = S.boxes.filter(function (x) { return x !== other; });
    });
    toast('已与' + (dir > 0 ? '下框' : '上框') + '合并', 'ok');
  }
  function createBox(rect) {
    if (!S.pageId) { return; }
    var nb = {
      id: S.tempId, x0: rect.x0, y0: rect.y0, x1: rect.x1, y1: rect.y1,
      flags: [], review: 0, note: ''
    };
    S.tempId += 1;
    mutate(function () { S.boxes.push(nb); S.selId = nb.id; });
    view.setArmed(false);
    setSel(nb.id);
    toast('已新建框', 'ok');
  }
  function toggleArmed() {
    if (!S.pageId) { toast('先在左栏选一页', 'warn'); return; }
    var on = !S.armed;
    view.setArmed(on);
    toast(on ? '新建框：在画布上拖出矩形（Esc 取消）' : '已退出新建框', 'ok');
  }

  function commitCoords() {
    var b = selBox();
    if (!b) { return; }
    var x0 = parseInt($('#inX0').value, 10);
    var y0 = parseInt($('#inY0').value, 10);
    var x1 = parseInt($('#inX1').value, 10);
    var y1 = parseInt($('#inY1').value, 10);
    if (isNaN(x0) || isNaN(y0) || isNaN(x1) || isNaN(y1)) { renderSelFields(); return; }
    if (x1 < x0) { var t = x0; x0 = x1; x1 = t; }
    if (y1 < y0) { var t2 = y0; y0 = y1; y1 = t2; }
    x0 = Math.round(clamp(x0, 0, S.page.w - 6));
    y0 = Math.round(clamp(y0, 0, S.page.h - 6));
    x1 = Math.round(clamp(x1, x0 + 6, S.page.w));
    y1 = Math.round(clamp(y1, y0 + 6, S.page.h));
    if (b.x0 === x0 && b.y0 === y0 && b.x1 === x1 && b.y1 === y1) { return; }
    mutate(function () { b.x0 = x0; b.y0 = y0; b.x1 = x1; b.y1 = y1; });
  }

  function commitNote() {
    var b = selBox();
    if (!b) { return; }
    var v = $('#inNote').value;
    if ((b.note || '') === v) { return; }
    mutate(function () { b.note = v; });
  }
  // ------------------------------------------------------------------ 右栏
  function renderInspector() {
    var b = selBox();
    var empty = $('#selEmpty'), body = $('#selBody');
    if (!b || !S.page) {
      empty.classList.remove('hidden');
      body.classList.add('hidden');
      $('#selIndexHint').textContent = '';
      $('#detailImg').removeAttribute('src');
      $('#detailOv').textContent = '';
      return;
    }
    empty.classList.add('hidden');
    body.classList.remove('hidden');
    var ord = readingOrder();
    var i = ord.indexOf(b);
    $('#selIndexHint').textContent = '#' + b.id + ' · ' + (i + 1) + '/' + ord.length;
    renderSelFields();
    buildDetail();
  }

  function renderSelFields() {
    var b = selBox();
    if (!b) { return; }
    var act = document.activeElement;
    var ids = ['inX0', 'inY0', 'inX1', 'inY1'];
    var vals = [b.x0, b.y0, b.x1, b.y1];
    for (var i = 0; i < ids.length; i++) {
      var inp = document.getElementById(ids[i]);
      if (inp && act !== inp) { inp.value = vals[i]; }
    }
    $('#selSize').textContent = (b.x1 - b.x0) + ' × ' + (b.y1 - b.y0) + ' px';
    var pill = $('#selReview');
    var map = { 0: ['未复核', 'pill'], 1: ['已确认', 'pill ok'], 2: ['存疑', 'pill warn'] };
    var m = map[b.review] || map[0];
    pill.textContent = m[0];
    pill.className = m[1];
    $('#selFlags').textContent = (b.flags && b.flags.length) ? ('标记：' + b.flags.join('、')) : '';
    if (act !== $('#inNote')) { $('#inNote').value = b.note || ''; }
  }

  function detailRect(b) {
    var w = b.x1 - b.x0, h = b.y1 - b.y0;
    var pw = S.page.w, ph = S.page.h;
    var ord = readingOrder();
    var i = ord.indexOf(b);
    var prev = i > 0 ? ord[i - 1] : null;
    var next = (i >= 0 && i < ord.length - 1) ? ord[i + 1] : null;
    var top = b.y0 - h * 0.9;
    var bot = b.y1 + h * 0.9;
    if (prev) { top = Math.min(top, prev.y0 - 6); }
    if (next) { bot = Math.max(bot, next.y1 + 6); }
    var left = b.x0 - w * 0.5;
    var right = b.x1 + w * 0.5;
    left = clamp(left, 0, pw); right = clamp(right, 0, pw);
    top = clamp(top, 0, ph); bot = clamp(bot, 0, ph);
    if (right - left < 24) { left = clamp(b.x0 - 12, 0, pw); right = clamp(b.x1 + 12, 0, pw); }
    if (bot - top < 24) { top = clamp(b.y0 - 12, 0, ph); bot = clamp(b.y1 + 12, 0, ph); }
    var scale = clamp(300 / Math.max(1, right - left), 1, 3);
    return {
      x0: Math.round(left), y0: Math.round(top),
      x1: Math.round(right), y1: Math.round(bot),
      scale: Math.round(scale * 100) / 100
    };
  }

  function buildDetail() {
    if (S.detailTimer) { clearTimeout(S.detailTimer); }
    S.detailTimer = setTimeout(doBuildDetail, 90);
  }

  function doBuildDetail() {
    S.detailTimer = null;
    var b = selBox();
    var img = $('#detailImg');
    if (!b || !S.page || !S.pageId) {
      S.detailRect = null;
      img.removeAttribute('src');
      $('#detailOv').textContent = '';
      return;
    }
    var R = detailRect(b);
    S.detailRect = R;
    var url = '/api/pages/' + encodeURIComponent(S.pageId) + '/crop?x0=' + R.x0 + '&y0=' + R.y0 +
      '&x1=' + R.x1 + '&y1=' + R.y1 + '&scale=' + R.scale + '&t=' + S.previewToken;
    if (img.getAttribute('src') !== url) {
      img.onload = function () { drawDetailOverlay(); };
      img.onerror = function () { $('#detailOv').textContent = ''; };
      img.src = url;
    } else {
      drawDetailOverlay();
    }
  }

  function drawDetailOverlay() {
    var ov = $('#detailOv'), img = $('#detailImg'), R = S.detailRect;
    ov.textContent = '';
    if (!R || !img.clientWidth || !img.clientHeight) { return; }
    var kx = img.clientWidth / Math.max(1, R.x1 - R.x0);
    var ky = img.clientHeight / Math.max(1, R.y1 - R.y0);
    S.boxes.forEach(function (b) {
      if (b.x1 <= R.x0 || b.x0 >= R.x1 || b.y1 <= R.y0 || b.y0 >= R.y1) { return; }
      var d = el('div', 'ov-box');
      if (b.id === S.selId) { d.className = 'ov-box sel'; }
      else if (b.flags && b.flags.indexOf('ai') >= 0) { d.className = 'ov-box ai'; }
      d.style.left = Math.round((b.x0 - R.x0) * kx) + 'px';
      d.style.top = Math.round((b.y0 - R.y0) * ky) + 'px';
      d.style.width = Math.max(2, Math.round((b.x1 - b.x0) * kx)) + 'px';
      d.style.height = Math.max(2, Math.round((b.y1 - b.y0) * ky)) + 'px';
      ov.appendChild(d);
    });
  }

  // ------------------------------------------------------------------ AI 建议
  function actionLabel(a) {
    return { tighten: '收紧', split: '拆分', merge: '合并', keep: '保持' }[a] || (a || '建议');
  }
  function propIndex(p, i) { return p.index != null ? p.index : i; }

  function loadProposals(pid) {
    S.proposals = [];
    renderAI();
    API.get('/api/pages/' + encodeURIComponent(pid) + '/proposals').then(function (r) {
      if (S.pageId !== pid) { return; }
      S.proposals = (r && r.proposals) || [];
      S.aiAvailable = !!(r && r.ai_available !== false);
      S.aiConfigured = !!(r && r.configured);
      renderAI();
    }).catch(function () {
      if (S.pageId !== pid) { return; }
      S.proposals = [];
      S.aiAvailable = false;
      renderAI();
    });
  }

  function muted(text) { return el('div', 'muted pad', text); }

  function renderAI() {
    var list = $('#aiList');
    list.textContent = '';
    var cnt = $('#aiCount');
    if (!S.pageId) { cnt.textContent = ''; list.appendChild(muted('选页后加载')); $('#btnAiAll').disabled = true; return; }
    var props = S.proposals || [];
    var pend = props.filter(function (p) { return !p.status || p.status === 'pending'; }).length;
    cnt.textContent = props.length ? (pend + ' 待处理 / ' + props.length) : '';
    $('#btnAiAll').disabled = !pend;
    if (!S.aiAvailable) {
      list.appendChild(muted('AI 复核不可用（后端 ai_refine 未就绪，或未配置 API Key）'));
    }
    if (!props.length) {
      list.appendChild(muted('暂无 AI 建议：点顶部「AI 复核」生成，或点「刷新」'));
      return;
    }
    props.forEach(function (p, i) {
      var card = el('div', 'ai-item ' + (p.status || 'pending'));
      var head = el('div', 'ai-head');
      var left = el('span', 'ai-title', '#' + (p.box_id != null ? p.box_id : '?') + ' · ' + actionLabel(p.action));
      head.appendChild(left);
      if (p.confidence != null) {
        head.appendChild(el('span', 'ai-conf', Math.round(p.confidence * 100) + '%'));
      }
      card.appendChild(head);
      if (p.reason) { card.appendChild(el('div', 'ai-reason', p.reason)); }
      var acts = el('div', 'ai-acts');
      if (!p.status || p.status === 'pending') {
        var ok = el('button', 'btn tiny ok', '接受');
        ok.addEventListener('click', function () { applyProposals([propIndex(p, i)], [], false); });
        var no = el('button', 'btn tiny', '拒绝');
        no.addEventListener('click', function () { applyProposals([], [propIndex(p, i)], false); });
        acts.appendChild(ok);
        acts.appendChild(no);
      } else {
        var st = { accepted: '已接受', rejected: '已拒绝', stale: '已失效' }[p.status] || p.status;
        acts.appendChild(el('span', 'muted', st));
      }
      card.appendChild(acts);
      list.appendChild(card);
    });
  }

  async function applyProposals(applyIdx, rejectIdx, applyAll) {
    if (!S.pageId) { return; }
    var pid = S.pageId;
    try {
      var res = await API.post('/api/pages/' + encodeURIComponent(pid) + '/proposals',
        { apply: applyIdx || [], reject: rejectIdx || [], apply_all: !!applyAll });
    } catch (e) {
      toast('应用 AI 建议失败', 'err', e.message);
      return;
    }
    if (S.pageId !== pid) { return; }
    adoptBoxes(res.boxes);
    if (res.proposals) { S.proposals = res.proposals; }
    S.dirty = false;
    setSaveState('saved');
    renderAI(); renderInspector(); renderHint(); renderPageInfo(); renderPages();
    toast('已应用 AI 建议', 'ok');
    if (res.warnings && res.warnings.length) { toast('提示', 'warn', res.warnings.join('\n')); }
  }

  function adoptBoxes(list) {
    S.boxes = (list || []).map(normBox);
    S.selId = null;
    S.tempId = S.boxes.reduce(function (m, b) { return Math.max(m, b.id || 0); }, 0) + 1;
    view.setData({ w: S.page.w, h: S.page.h, boxes: S.boxes, selId: null, columns: S.page.columns });
  }

  // ------------------------------------------------------------------ 状态条
  function renderHint() {
    var nbox = S.boxes.length;
    var nrev = S.boxes.filter(function (b) { return !!b.review; }).length;
    var b = selBox();
    var t = S.pageId ? (nbox + ' 框 · 已复核 ' + nrev) : '未选页';
    if (b) { t += ' · 选中 #' + b.id + ' (' + b.x0 + ',' + b.y0 + ')-(' + b.x1 + ',' + b.y1 + ')'; }
    if (S.armed) { t += ' · 新建模式（拖框）'; }
    $('#hintBoxes').textContent = t;
  }
  function updateHintPos(m) {
    var p = $('#hintPos');
    if (!m) { p.textContent = '—'; return; }
    p.textContent = 'x ' + Math.round(m.x) + ', y ' + Math.round(m.y);
  }

  function renderPageInfo() {
    var box = $('#pageInfo');
    box.textContent = '';
    if (!S.pageId || !S.page) { box.appendChild(muted('未选择页面')); return; }
    var p = pageById(S.pageId) || {};
    var st = statusInfo(p);
    var rows = [
      ['文件', p.name || S.pageId],
      ['状态', st.label + (p.error ? ('（' + p.error + '）') : '')],
      ['尺寸', S.page.w + ' × ' + S.page.h + ' px'],
      ['框数', String(S.boxes.length)],
      ['已复核', S.boxes.filter(function (b) { return !!b.review; }).length + ' / ' + S.boxes.length],
      ['AI 复核', p.ai_refined ? '已跑过' : (S.aiAvailable ? '可用' : '不可用')]
    ];
    rows.forEach(function (r) {
      var line = el('div', 'kv');
      line.appendChild(el('span', 'k', r[0]));
      line.appendChild(el('span', 'v', r[1]));
      box.appendChild(line);
    });
  }

  // ------------------------------------------------------------------ 任务
  function kindLabel(k) {
    return { process: '一键处理', export: '一键切图', 'ai-refine': 'AI 复核' }[k] || (k || '任务');
  }

  function renderJobbar() {
    var bar = $('#jobbar');
    var act = S.jobOrder.map(function (id) { return S.jobs[id]; }).filter(function (j) { return j && j.state === 'running'; });
    if (!act.length) { bar.classList.add('hidden'); S.activeJobId = null; return; }
    var j = act[0];
    bar.classList.remove('hidden');
    S.activeJobId = j.id;
    $('#jobKind').textContent = j.label || kindLabel(j.kind);
    var msg = j.message || '进行中…';
    if (j.stage) { msg = j.stage + ' · ' + msg; }
    $('#jobMsg').textContent = msg;
    var frac = 0;
    if (j.curPid && typeof j.frac === 'number') { frac = (j.done + clamp(j.frac, 0, 1)) / Math.max(1, j.total); }
    else if (j.total > 0) { frac = j.done / j.total; }
    $('#jobFill').style.width = Math.round(clamp(frac, 0, 1) * 100) + '%';
    $('#jobCount').textContent = j.done + '/' + j.total + (act.length > 1 ? ('  (+' + (act.length - 1) + ')') : '');
  }

  function trackJob(jid, meta) {
    var rec = {
      id: jid, kind: (meta && meta.kind) || 'job', label: (meta && meta.label) || '',
      state: 'running', done: 0, total: 1, message: '', errors: [],
      pageIds: (meta && meta.pageIds) || null, curPid: null, frac: 0, stage: '', resolve: null
    };
    S.jobs[jid] = rec;
    if (S.jobOrder.indexOf(jid) < 0) { S.jobOrder.push(jid); }
    var promise = new Promise(function (resolve) { rec.resolve = resolve; });
    ensurePoll();
    renderJobbar();
    return promise;
  }

  function ensurePoll() {
    if (S.pollTimer) { return; }
    S.pollTimer = setTimeout(pollTick, 400);
  }

  async function pollTick() {
    S.pollTimer = null;
    var ids = S.jobOrder.filter(function (id) { return S.jobs[id] && S.jobs[id].state === 'running'; });
    if (!ids.length) { renderJobbar(); renderPages(); return; }
    for (var i = 0; i < ids.length; i++) {
      var jid = ids[i];
      var rec = S.jobs[jid];
      if (!rec) { continue; }
      try {
        var j = await API.get('/api/jobs/' + encodeURIComponent(jid));
        if (!j) { continue; }
        rec.state = j.state || 'running';
        rec.done = j.done || 0;
        rec.total = j.total || 1;
        rec.message = j.message || '';
        rec.errors = j.errors || [];
        if (j.current) {
          rec.curPid = j.current.pid || null;
          rec.frac = typeof j.current.frac === 'number' ? j.current.frac : 0;
          rec.stage = j.current.stage || '';
          if (rec.curPid) { S.jobProgress[rec.curPid] = { frac: rec.frac, kind: rec.kind }; }
        }
        if (rec.state !== 'running') { onJobDone(rec); }
      } catch (e) {
        rec.state = 'error';
        rec.message = e.message;
        onJobDone(rec);
      }
    }
    renderJobbar();
    renderPages();
    var more = S.jobOrder.some(function (id) { return S.jobs[id] && S.jobs[id].state === 'running'; });
    if (more) { ensurePoll(); }
  }

  function clearJobProgress(rec) {
    if (rec.curPid) { delete S.jobProgress[rec.curPid]; }
    if (rec.pageIds) { rec.pageIds.forEach(function (pid) { delete S.jobProgress[pid]; }); }
  }

  function onJobDone(rec) {
    clearJobProgress(rec);
    if (rec.state === 'done') {
      if (rec.kind === 'process') { toast('处理完成', 'ok'); refreshAfterJob(rec.pageIds, true); }
      else if (rec.kind === 'export') { toast('导出完成', 'ok'); }
      else if (rec.kind === 'ai-refine') { toast('AI 复核完成', 'ok'); refreshAfterJob(rec.pageIds, false); }
    } else if (rec.state === 'canceled') {
      toast('任务已取消', 'warn');
    } else {
      toast('任务失败', 'err', (rec.errors && rec.errors.join('\n')) || rec.message || '未知错误');
    }
    var done = rec.resolve;
    rec.resolve = null;
    if (done) { done(rec); }
    var jid = rec.id;
    setTimeout(function () {
      delete S.jobs[jid];
      S.jobOrder = S.jobOrder.filter(function (x) { return x !== jid; });
      renderJobbar();
    }, 6000);
  }

  async function refreshAfterJob(pageIds, reloadBoxes) {
    try { var st = await API.get('/api/state'); applyState(st); } catch (e) { /* ignore */ }
    if (reloadBoxes && S.pageId && (!pageIds || pageIds.indexOf(S.pageId) >= 0) && !S.dirty) {
      reloadCurrentPage();
    }
    if (S.pageId && (!pageIds || pageIds.indexOf(S.pageId) >= 0)) {
      loadProposals(S.pageId);
      if (!reloadBoxes) { reloadBoxesQuiet(); }
    }
  }

  async function reloadBoxesQuiet() {
    if (!S.pageId || S.dirty) { return; }
    var pid = S.pageId;
    try { var res = await API.get('/api/pages/' + encodeURIComponent(pid)); } catch (e) { return; }
    if (S.pageId !== pid || S.dirty) { return; }
    S.boxes = (res.boxes || []).map(normBox);
    S.pageStatus = res.status || '';
    S.selId = null;
    view.setData({ w: S.page.w, h: S.page.h, boxes: S.boxes, selId: null, columns: S.page.columns });
    renderInspector(); renderHint(); renderPageInfo(); renderPages();
  }

  function cancelJob() {
    if (!S.activeJobId) { return; }
    API.post('/api/jobs/' + encodeURIComponent(S.activeJobId) + '/cancel', {}).then(function () {
      toast('已请求取消', 'warn');
    }).catch(function (e) { toast('取消失败', 'err', e.message); });
  }

  async function startProcess(ids) {
    var res;
    try { res = await API.post('/api/pages/process', { ids: ids }); }
    catch (e) { toast('启动处理失败', 'err', e.message); return; }
    if (!res || !res.job_id) { toast('后端没有返回任务号', 'err'); return; }
    await trackJob(res.job_id, { kind: 'process', label: '一键处理', pageIds: ids === 'all' ? null : ids });
  }

  async function startAIRefine(pids, scope, mode) {
    for (var i = 0; i < pids.length; i++) {
      var pid = pids[i];
      var idsArg = scope === 'all' ? 'all' : 'suspect';
      if (scope === 'selected') {
        if (pid !== S.pageId || S.selId == null) { toast('「仅选中框」需先在当前页选中一个框', 'warn'); return; }
        idsArg = [S.selId];
      }
      var res;
      try { res = await API.post('/api/pages/' + encodeURIComponent(pid) + '/ai-refine', { ids: idsArg, mode: mode }); }
      catch (e) { toast('启动 AI 复核失败', 'err', e.message); return; }
      if (!res || !res.job_id) { toast('后端没有返回任务号', 'err'); return; }
      var rec = await trackJob(res.job_id, { kind: 'ai-refine', label: 'AI 复核', pageIds: [pid] });
      if (rec.state !== 'done') { return; }
    }
  }

  async function startExport(pids, opts) {
    var ok = 0;
    for (var i = 0; i < pids.length; i++) {
      var res;
      try { res = await API.post('/api/pages/' + encodeURIComponent(pids[i]) + '/export', opts); }
      catch (e) {
        var p = pageById(pids[i]);
        toast('导出失败：' + ((p && p.name) || pids[i]), 'err', e.message);
        return;
      }
      if (!res || !res.job_id) { toast('后端没有返回任务号', 'err'); return; }
      var rec = await trackJob(res.job_id, { kind: 'export', label: '一键切图', pageIds: [pids[i]] });
      if (rec.state !== 'done') { return; }
      ok += 1;
    }
    if (ok) { toast('导出完成：' + ok + ' 页', 'ok', opts.out || '（后端默认目录）'); }
  }

  function checkedIds() {
    return Object.keys(S.checked).filter(function (k) { return S.checked[k]; });
  }
  function targetPages(scope) {
    if (scope === 'current') { return S.pageId ? [S.pageId] : []; }
    if (scope === 'checked') { return checkedIds(); }
    return S.pages.map(function (p) { return p.id; });
  }
  // ------------------------------------------------------------------ 导入 / 移除
  async function importFiles() {
    var r;
    try { r = await API.get('/api/dialog/files'); }
    catch (e) { toast('打开文件选择框失败', 'err', e.message); return; }
    var paths = (r && r.paths) || [];
    if (!paths.length) { toast('没有选择文件', 'warn'); return; }
    addPages({ paths: paths });
  }

  async function importFolder() {
    var r;
    try { r = await API.get('/api/dialog/folder'); }
    catch (e) { toast('打开文件夹选择框失败', 'err', e.message); return; }
    if (!r || r.canceled || !r.path) { toast('没有选择文件夹', 'warn'); return; }
    addPages({ folder: r.path, pattern: '*.tif*' });
  }

  async function addPages(body) {
    var res;
    try { res = await API.post('/api/pages/add', body); }
    catch (e) { toast('导入失败', 'err', e.message); return; }
    var added = (res && res.added) || [];
    var skipped = (res && res.skipped) || [];
    try { var st = await API.get('/api/state'); applyState(st); } catch (e2) { /* ignore */ }
    var detail = skipped.length ? ('跳过 ' + skipped.length + ' 个：\n' + skipped.map(function (s) { return (s.path || s) + ' — ' + (s.reason || ''); }).join('\n')) : '';
    toast('导入 ' + added.length + ' 页', added.length ? 'ok' : 'warn', detail);
    if (added.length) { selectPage(added[0]); }
  }

  function removeChecked() {
    var ids = checkedIds();
    if (!ids.length) { return; }
    confirmDialog('移除页面', '从项目中移除 ' + ids.length + ' 页？（不会删除源文件）', async function () {
      for (var i = 0; i < ids.length; i++) {
        try { await API.del('/api/pages/' + encodeURIComponent(ids[i])); }
        catch (e) { toast('移除失败', 'err', e.message); }
      }
      S.checked = {};
      if (S.pageId && ids.indexOf(S.pageId) >= 0) { S.pageId = null; clearPage(); }
      try { var st = await API.get('/api/state'); applyState(st); } catch (e2) { /* ignore */ }
      toast('已移除 ' + ids.length + ' 页', 'ok');
    });
  }

  // ------------------------------------------------------------------ 弹层
  function showModal(sel) {
    var dlg = $(sel);
    if (!dlg) { return; }
    hideAllModals();
    dlg.classList.add('show');
    $('#modalMask').classList.add('show');
    S.modalOpen = sel;
    var first = dlg.querySelector('input, select, button');
    if (first) { setTimeout(function () { try { first.focus(); } catch (e) { /* ignore */ } }, 40); }
  }
  function hideAllModals() {
    $$('.dlg.show').forEach(function (d) { d.classList.remove('show'); });
    $('#modalMask').classList.remove('show');
    S.modalOpen = null;
  }
  function hideModal(sel) {
    var dlg = $(sel);
    if (dlg) { dlg.classList.remove('show'); }
    if (!$$('.dlg.show').length) { $('#modalMask').classList.remove('show'); S.modalOpen = null; }
  }
  var confirmCb = null;
  function confirmDialog(title, text, onOk) {
    $('#confirmTitle').textContent = title;
    $('#confirmText').textContent = text;
    confirmCb = onOk;
    showModal('#dlgConfirm');
  }

  // ------------------------------------------------------------------ 设置抽屉
  function openDrawer() {
    fillSettings();
    $('#drawer').classList.add('open');
    $('#drawerMask').classList.add('show');
  }
  function closeDrawer() {
    $('#drawer').classList.remove('open');
    $('#drawerMask').classList.remove('show');
  }
  function fillSettings() {
    var ai = (S.settings && S.settings.ai) || {};
    var def = (S.settings && S.settings.defaults) || {};
    var key = $('#setKey');
    key.value = '';
    key.placeholder = ai.configured ? ('已设置：' + (ai.api_key || '') + '（留空不修改）') : '未设置 API Key';
    $('#setBase').value = ai.base_url || 'https://api.deepseek.com';
    $('#setModel').value = ai.model || 'deepseek-flash';
    $('#setWorkers').value = ai.max_workers != null ? ai.max_workers : 4;
    $('#setPad').value = def.pad_ratio != null ? def.pad_ratio : 0.1;
    $('#setMinPad').value = def.min_pad != null ? def.min_pad : 6;
    $('#setOrder').value = def.order || 'visual';
    $('#setFmt').value = def.fmt || 'png';
    $('#setProjectPath').value = (S.project && S.project.path) || '';
    $('#aiStatus').textContent = ai.configured ? 'AI 已配置' : 'AI 未配置';
    $('#aiStatus').className = 'chip ' + (ai.configured ? 'ok' : 'warn');
    $('#aiTestMsg').textContent = '';
    $('#aiTestMsg').className = 'testmsg';
  }
  async function saveSettings() {
    var ai = {
      base_url: $('#setBase').value.trim(),
      model: $('#setModel').value.trim(),
      max_workers: Math.round(num($('#setWorkers').value, 4))
    };
    var key = $('#setKey').value.trim();
    if (key) { ai.api_key = key; }
    var defaults = {
      pad_ratio: num($('#setPad').value, 0.1),
      min_pad: Math.round(num($('#setMinPad').value, 6)),
      order: $('#setOrder').value,
      fmt: $('#setFmt').value
    };
    try { await API.post('/api/settings', { ai: ai, defaults: defaults }); }
    catch (e) { toast('保存设置失败', 'err', e.message); return; }
    toast('设置已保存', 'ok');
    try { var st = await API.get('/api/state'); applyState(st); } catch (e2) { /* ignore */ }
    fillSettings();
  }
  async function testAI() {
    var btn = $('#btnTestAI');
    var old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '测试中…';
    var body = {
      ai: {
        api_key: $('#setKey').value.trim(),
        base_url: $('#setBase').value.trim(),
        model: $('#setModel').value.trim()
      }
    };
    try {
      var r = await API.post('/api/ai/test', body);
      var ok = r && r.ok;
      $('#aiTestMsg').textContent = (r && r.message) || (ok ? '连接成功' : '连接失败');
      $('#aiTestMsg').className = 'testmsg ' + (ok ? 'ok' : 'err');
    } catch (e) {
      $('#aiTestMsg').textContent = '测试失败：' + e.message;
      $('#aiTestMsg').className = 'testmsg err';
    }
    btn.disabled = false;
    btn.textContent = old;
  }
  async function openProject() {
    var p = $('#setProjectPath').value.trim();
    if (!p) { toast('请填写项目目录', 'warn'); return; }
    try {
      var r = await API.post('/api/project/open', { path: p });
      if (r && r.state) { applyState(r.state); } else { try { applyState(await API.get('/api/state')); } catch (e2) { /* ignore */ } }
      toast('已打开项目', 'ok');
      closeDrawer();
    } catch (e) { toast('打开项目失败', 'err', e.message); }
  }

  // ------------------------------------------------------------------ 导出 / AI 对话框
  function openExport() {
    if (!S.pages.length) { toast('还没有页面', 'warn'); return; }
    var def = (S.settings && S.settings.defaults) || {};
    $('#exOut').value = S.lastExportOut || '';
    $('#exPad').value = def.pad_ratio != null ? def.pad_ratio : 0.1;
    $('#exMin').value = def.min_pad != null ? def.min_pad : 6;
    $('#exOrder').value = def.order || 'visual';
    $('#exFmt').value = def.fmt || 'png';
    $('#exScope').value = 'current';
    showModal('#dlgExport');
  }
  async function chooseExportDir() {
    try {
      var r = await API.get('/api/dialog/export_dir');
      if (r && r.path) { $('#exOut').value = r.path; }
    } catch (e) { toast('选择目录失败', 'err', e.message); }
  }
  function doExport() {
    var ids = targetPages($('#exScope').value);
    if (!ids.length) { toast('没有要导出的页（先勾选或选「当前页」）', 'warn'); return; }
    var opts = {
      out: $('#exOut').value.trim(),
      pad_ratio: num($('#exPad').value, 0.1),
      min_pad: Math.round(num($('#exMin').value, 6)),
      order: $('#exOrder').value,
      fmt: $('#exFmt').value
    };
    S.lastExportOut = opts.out;
    hideModal('#dlgExport');
    startExport(ids, opts);
  }

  function openAI() {
    if (!S.pages.length) { toast('还没有页面', 'warn'); return; }
    $('#aiScope').value = 'suspect';
    $('#aiMode').value = 'auto';
    $('#aiPages').value = 'current';
    showModal('#dlgAI');
  }
  function doAIRefine() {
    var pids = targetPages($('#aiPages').value);
    if (!pids.length) { toast('没有要复核的页', 'warn'); return; }
    var scope = $('#aiScope').value;
    var mode = $('#aiMode').value;
    hideModal('#dlgAI');
    startAIRefine(pids, scope, mode);
  }

  // ------------------------------------------------------------------ 键盘
  function isTypingTarget(t) {
    if (!t || !t.tagName) { return false; }
    var n = t.tagName.toUpperCase();
    return n === 'INPUT' || n === 'TEXTAREA' || n === 'SELECT' || t.isContentEditable;
  }
  function onKeyDown(e) {
    if (e.key === 'Escape') {
      if (S.modalOpen) { hideModal(S.modalOpen); return; }
      if ($('#drawer').classList.contains('open')) { closeDrawer(); return; }
      if (S.armed) { view.setArmed(false); return; }
      if (S.selId != null) { setSel(null); return; }
    }
    if (isTypingTarget(e.target)) { return; }
    if (S.modalOpen || $('#drawer').classList.contains('open')) { return; }
    var mod = e.ctrlKey || e.metaKey;
    if (mod && (e.key === 'z' || e.key === 'Z')) { e.preventDefault(); if (e.shiftKey) { redo(); } else { undo(); } return; }
    if (mod && (e.key === 'y' || e.key === 'Y')) { e.preventDefault(); redo(); return; }
    if (mod && (e.key === 's' || e.key === 'S')) { e.preventDefault(); flushSave(); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) { return; }
    switch (e.key) {
      case 'Delete':
      case 'Backspace': e.preventDefault(); deleteSel(); break;
      case 'a':
      case 'A': e.preventDefault(); toggleArmed(); break;
      case 's':
      case 'S': e.preventDefault(); splitSel(); break;
      case 'm': e.preventDefault(); mergeSel(1); break;
      case 'M': e.preventDefault(); mergeSel(-1); break;
      case '1': setReview(1); break;
      case '2': setReview(2); break;
      case '0': setReview(0); break;
      case 'ArrowLeft':
      case 'ArrowUp': e.preventDefault(); stepSel(-1); break;
      case 'ArrowRight':
      case 'ArrowDown': e.preventDefault(); stepSel(1); break;
      case '?': e.preventDefault(); showModal('#dlgKeys'); break;
      case '+':
      case '=': e.preventDefault(); view.setZoomAt(view.zoom * 1.25); break;
      case '-':
      case '_': e.preventDefault(); view.setZoomAt(view.zoom / 1.25); break;
      case 'f':
      case 'F': e.preventDefault(); view.resetView(); break;
      default: break;
    }
  }

  // ------------------------------------------------------------------ 绑定
  function bindAll() {
    $('#btnImportFiles').addEventListener('click', importFiles);
    $('#btnImportFolder').addEventListener('click', importFolder);
    $('#btnProcess').addEventListener('click', function () {
      if (!S.pages.length) { toast('还没有页面', 'warn'); return; }
      var ids = checkedIds();
      var n = ids.length || S.pages.length;
      var text = ids.length ? ('处理勾选的 ' + n + ' 页？') : ('处理全部 ' + n + ' 页？整页分割约 7–13 秒/页。');
      confirmDialog('一键处理', text, function () { startProcess(ids.length ? ids : 'all'); });
    });
    $('#btnAI').addEventListener('click', openAI);
    $('#btnExport').addEventListener('click', openExport);
    $('#btnSettings').addEventListener('click', openDrawer);
    $('#jobCancel').addEventListener('click', cancelJob);

    $('#chkAll').addEventListener('change', function (e) {
      var on = e.target.checked;
      S.pages.filter(function (p) { return !S.filter || statusInfo(p).key === S.filter; }).forEach(function (p) { S.checked[p.id] = on; });
      renderPages();
    });
    $('#statusFilter').addEventListener('change', function (e) { S.filter = e.target.value; renderPages(); });
    $('#btnRemove').addEventListener('click', removeChecked);

    $('#btnConfirm').addEventListener('click', function () { setReview(1); });
    $('#btnSuspect').addEventListener('click', function () { setReview(2); });
    $('#btnReset').addEventListener('click', function () { setReview(0); });
    $('#btnDelete').addEventListener('click', deleteSel);
    $('#btnSplit').addEventListener('click', splitSel);
    $('#btnMergeNext').addEventListener('click', function () { mergeSel(1); });
    $('#btnMergePrev').addEventListener('click', function () { mergeSel(-1); });
    $('#btnAdd').addEventListener('click', toggleArmed);
    ['inX0', 'inY0', 'inX1', 'inY1'].forEach(function (id) {
      var inp = document.getElementById(id);
      inp.addEventListener('change', commitCoords);
      inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') { inp.blur(); } });
    });
    $('#inNote').addEventListener('change', commitNote);
    $('#inNote').addEventListener('keydown', function (e) { if (e.key === 'Enter') { $('#inNote').blur(); } });

    $('#btnAiAll').addEventListener('click', function () { applyProposals([], [], true); });
    $('#btnAiRefresh').addEventListener('click', function () {
      if (!S.pageId) { return; }
      loadProposals(S.pageId);
      toast('已刷新 AI 建议', 'ok');
    });
    $('#btnProcessOne').addEventListener('click', function () {
      if (!S.pageId) { return; }
      confirmDialog('处理本页', '对当前页跑分割？', function () { startProcess([S.pageId]); });
    });
    $('#btnAiOne').addEventListener('click', openAI);
    $('#btnExportOne').addEventListener('click', openExport);
    $('#btnReload').addEventListener('click', reloadCurrentPage);
    $('#btnFit').addEventListener('click', function () { view.resetView(); });
    $('#btnKeys').addEventListener('click', function () { showModal('#dlgKeys'); });

    $('#drawerMask').addEventListener('click', closeDrawer);
    $('#btnCloseDrawer').addEventListener('click', closeDrawer);
    $('#btnSaveSettings').addEventListener('click', saveSettings);
    $('#btnTestAI').addEventListener('click', testAI);
    $('#btnOpenProject').addEventListener('click', openProject);

    $('#modalMask').addEventListener('click', hideAllModals);
    $('#btnExportCancel').addEventListener('click', function () { hideModal('#dlgExport'); });
    $('#btnExportCancel2').addEventListener('click', function () { hideModal('#dlgExport'); });
    $('#btnExportGo').addEventListener('click', doExport);
    $('#btnChooseOut').addEventListener('click', chooseExportDir);
    $('#btnAiCancel').addEventListener('click', function () { hideModal('#dlgAI'); });
    $('#btnAiCancel2').addEventListener('click', function () { hideModal('#dlgAI'); });
    $('#btnAiGo').addEventListener('click', doAIRefine);
    $('#btnKeysClose').addEventListener('click', function () { hideModal('#dlgKeys'); });
    $('#btnKeysClose2').addEventListener('click', function () { hideModal('#dlgKeys'); });
    $('#btnConfirmOk').addEventListener('click', function () {
      hideModal('#dlgConfirm');
      var cb = confirmCb;
      confirmCb = null;
      if (cb) { cb(); }
    });
    $('#btnConfirmCancel').addEventListener('click', function () { confirmCb = null; hideModal('#dlgConfirm'); });
    $('#btnConfirmCancel2').addEventListener('click', function () { confirmCb = null; hideModal('#dlgConfirm'); });

    document.addEventListener('keydown', onKeyDown);
  }

  // ------------------------------------------------------------------ 启动
  function onZoomPct(pct) {
    $('#zoomLabel').textContent = pct + '%';
    if (!S.page || !S.pageId || !view.scale) { return; }
    if (view.scale > 0.5 && S.previewW < 3200) {
      S.previewW = 3200;
      view.setImage(previewUrl(S.pageId, 3200), { keepView: true });
    }
  }

  function boot() {
    view = new window.YI.CanvasView({
      canvas: $('#cv'),
      wrap: $('#canvasWrap'),
      onSelect: function (id) { setSel(id); },
      onEditStart: function () { S.lastSnap = snapshot(); pushUndo(); },
      onEditChange: function () {
        setSaveState('dirty');
        renderSelFields();
        renderHint();
        if (S.previewW < 3200 && S.page && S.page.w) { /* noop */ }
      },
      onEditEnd: function () {
        if (snapshot() === S.lastSnap) {
          if (S.hist.length) { S.hist.pop(); }
          if (!S.dirty) { setSaveState('saved'); }
        } else {
          S.dirty = true;
          setSaveState('dirty');
          scheduleSave();
        }
        renderInspector();
        renderHint();
      },
      onCreate: function (rect) { createBox(rect); },
      onMouseMove: function (m) { updateHintPos(m); },
      onZoom: function (pct) { onZoomPct(pct); },
      onImageLoad: function () { setStageHint(''); },
      onImageError: function (url) {
        if (retryPreviewWithRebuild(url)) { return; }
        setStageHint('预览图加载失败。可点右栏「重新载入」重建，或先「处理本页」。');
        toast('预览图加载失败', 'err', '点右栏「重新载入」可重建预览');
      },
      onArmedChange: function (on) {
        S.armed = on;
        document.body.classList.toggle('armed', on);
        $('#btnAdd').classList.toggle('active', on);
        renderHint();
      }
    });
    bindAll();
    setSaveState('idle');
    setStageHint('正在连接后端…');
    loadState();
    window.addEventListener('beforeunload', function (e) {
      if (S.dirty) { e.preventDefault(); e.returnValue = ''; }
    });
    if (window.ResizeObserver) {
      new ResizeObserver(function () { drawDetailOverlay(); }).observe($('#detailImg'));
    }
    window.__yi = {
      get view() { return view; },
      get state() { return S; },
      select: setSel
    };
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();