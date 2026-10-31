/* canvas.js —— 标注画布：页面图 + 自绘框 + 缩放/平移/拖动/8 点缩放/框选新建。
   不依赖框架与 CDN；所有回调由 app.js 提供，画布自身不持久化任何数据。 */
(function () {
  'use strict';
  var HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
  var CURSORS = {
    nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize',
    n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize'
  };
  var MIN_SIZE = 6;
  var HS = 9;

  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function rnd(v) { return Math.round(v); }

  function View(o) {
    this.canvas = o.canvas;
    this.wrap = o.wrap;
    this.opts = o;
    this.ctx = this.canvas.getContext('2d');
    this.cw = 1; this.ch = 1; this.dpr = 1;
    this.data = { w: 0, h: 0, boxes: [], selId: null, columns: [] };
    this.img = null; this.imgUrl = ''; this.imgOk = false; this.imgErr = false;
    this.fitScale = 1; this.zoom = 1; this.scale = 1; this.tx = 0; this.ty = 0;
    this.mode = 'idle'; this.drag = null; this.create = null;
    this.space = false; this.armed = false;
    this.hoverId = null; this.mouse = null;
    this.showCols = true; this.hoverCol = -1;
    this.raf = 0;
    this._bind();
    this._resize();
  }

  View.prototype.invalidate = function () {
    if (this.raf) { return; }
    var self = this;
    this.raf = window.requestAnimationFrame(function () { self.raf = 0; self.render(); });
  };

  View.prototype.setData = function (d) {
    var keep = !!(this.data.w && this.data.h && d.w === this.data.w && d.h === this.data.h);
    this.data = { w: d.w || 0, h: d.h || 0, boxes: d.boxes || [], selId: d.selId === undefined ? null : d.selId, columns: d.columns || [] };
    this._renumber();
    this._recalcFit();
    if (!keep) { this.resetView(); } else { this._applyTransform(); this.invalidate(); }
  };

  View.prototype.setSelId = function (id) {
    if (this.data.selId === id) { return; }
    this.data.selId = id;
    this.invalidate();
  };

  View.prototype.setImage = function (url, opts) {
    if (!url) {
      this.img = null; this.imgUrl = ''; this.imgOk = false; this.imgErr = false;
      this.invalidate();
      return;
    }
    if (url === this.imgUrl && this.imgOk) { return; }
    var self = this;
    var keepView = !!(opts && opts.keepView);
    this.imgUrl = url; this.imgOk = false; this.imgErr = false;
    var im = new Image();
    im.onload = function () {
      if (self.imgUrl !== url) { return; }
      self.img = im; self.imgOk = true; self.imgErr = false;
      if (!keepView) { self.resetView(); }
      self.invalidate();
      if (self.opts.onImageLoad) { self.opts.onImageLoad(); }
    };
    im.onerror = function () {
      if (self.imgUrl !== url) { return; }
      self.img = null; self.imgOk = false; self.imgErr = true;
      self.invalidate();
      if (self.opts.onImageError) { self.opts.onImageError(url); }
    };
    im.src = url;
  };

  View.prototype.setArmed = function (on) {
    this.armed = !!on;
    this._cursor(null);
    this._hintCreate();
    this.invalidate();          // 进入/退出新建模式时立刻重绘十字参考线
  };

  View.prototype._hintCreate = function () {
    if (this.opts.onArmedChange) { this.opts.onArmedChange(this.armed); }
  };

  View.prototype.getMouseImg = function () {
    return this.mouse ? { x: this.mouse.x, y: this.mouse.y } : null;
  };

  View.prototype.zoomPct = function () { return Math.round(this.zoom * 100); };

  View.prototype._resize = function () {
    var r = this.wrap.getBoundingClientRect();
    // 必须先按「旧」尺寸算画面中心点，否则侧栏折叠/展开后图像会偏到一边
    var oldCw = Math.max(1, this.cw), oldCh = Math.max(1, this.ch);
    var oldScale = this.scale || 1, oldTx = this.tx || 0, oldTy = this.ty || 0;
    this.dpr = window.devicePixelRatio || 1;
    this.cw = Math.max(1, Math.round(r.width));
    this.ch = Math.max(1, Math.round(r.height));
    this.canvas.width = Math.round(this.cw * this.dpr);
    this.canvas.height = Math.round(this.ch * this.dpr);
    this.canvas.style.width = this.cw + 'px';
    this.canvas.style.height = this.ch + 'px';
    if (!this.data.w || !this.data.h || !this.scale) {
      this._recalcFit();
      this.zoom = 1;
      this._applyTransform();
      this.tx = (this.cw - this.data.w * this.scale) / 2;
      this.ty = (this.ch - 26 - this.data.h * this.scale) / 2;
      this.invalidate();
      if (this.opts.onZoom) { this.opts.onZoom(this.zoomPct()); }
      return;
    }
    var center = { x: (oldCw / 2 - oldTx) / oldScale, y: (oldCh / 2 - oldTy) / oldScale };
    this._recalcFit();
    this._applyTransform();
    this.tx = this.cw / 2 - center.x * this.scale;
    this.ty = this.ch / 2 - center.y * this.scale;
    this.invalidate();
  };

  View.prototype._recalcFit = function () {
    var d = this.data;
    if (!d.w || !d.h) { this.fitScale = 1; return; }
    var fw = (this.cw - 56) / d.w;
    var fh = (this.ch - 84) / d.h;
    this.fitScale = clamp(Math.min(fw, fh), 0.005, 4);
  };

  View.prototype._applyTransform = function () { this.scale = this.fitScale * this.zoom; };

  View.prototype.resetView = function () {
    var d = this.data;
    this._recalcFit();
    this.zoom = 1;
    this._applyTransform();
    if (d.w && d.h) {
      this.tx = (this.cw - d.w * this.scale) / 2;
      this.ty = (this.ch - 26 - d.h * this.scale) / 2;
    } else {
      this.tx = this.cw / 2; this.ty = this.ch / 2;
    }
    this.invalidate();
    if (this.opts.onZoom) { this.opts.onZoom(this.zoomPct()); }
  };

  View.prototype.setZoomAt = function (z, sx, sy) {
    if (sx === undefined) { sx = this.cw / 2; sy = this.ch / 2; }
    var before = this.imgFromScreen(sx, sy);
    this.zoom = clamp(z, 0.2, 5);
    this._applyTransform();
    this.tx = sx - before.x * this.scale;
    this.ty = sy - before.y * this.scale;
    this.invalidate();
    if (this.opts.onZoom) { this.opts.onZoom(this.zoomPct()); }
  };

  View.prototype.screenPos = function (ev) {
    var r = this.canvas.getBoundingClientRect();
    return { x: ev.clientX - r.left, y: ev.clientY - r.top };
  };

  View.prototype.imgFromScreen = function (sx, sy) {
    return { x: (sx - this.tx) / this.scale, y: (sy - this.ty) / this.scale };
  };

  // 新建框时的指针参考虚线：贯穿画布的十字准线 + 拖拽锚点延长线
  View.prototype._drawGuides = function () {
    if (!this.armed || !this.mouse) { return; }
    var ctx = this.ctx;
    var x = Math.round(this.mouse.sx) + 0.5;
    var y = Math.round(this.mouse.sy) + 0.5;
    ctx.save();
    ctx.setLineDash([6, 5]);
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(56,189,248,0.70)';
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, this.ch); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(this.cw, y); ctx.stroke();
    if (this.mode === 'create' && this.create) {
      // 锚点一侧也延长一条，方便对齐另一条边
      var ax = Math.round(this.create.x0 * this.scale + this.tx) + 0.5;
      var ay = Math.round(this.create.y0 * this.scale + this.ty) + 0.5;
      ctx.globalAlpha = 0.55;
      ctx.beginPath(); ctx.moveTo(ax, 0); ctx.lineTo(ax, this.ch); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, ay); ctx.lineTo(this.cw, ay); ctx.stroke();
    }
    ctx.restore();
  };

  View.prototype.rectToScreen = function (x0, y0, x1, y1) {
    return {
      x: x0 * this.scale + this.tx, y: y0 * this.scale + this.ty,
      w: (x1 - x0) * this.scale, h: (y1 - y0) * this.scale
    };
  };

  View.prototype.selBox = function () {
    var id = this.data.selId;
    if (id === null || id === undefined) { return null; }
    for (var i = 0; i < this.data.boxes.length; i++) {
      if (this.data.boxes[i].id === id) { return this.data.boxes[i]; }
    }
    return null;
  };

  View.prototype.handlePoints = function (r) {
    var mx = r.x + r.w / 2, my = r.y + r.h / 2;
    return [
      { name: 'nw', x: r.x, y: r.y },
      { name: 'n', x: mx, y: r.y },
      { name: 'ne', x: r.x + r.w, y: r.y },
      { name: 'e', x: r.x + r.w, y: my },
      { name: 'se', x: r.x + r.w, y: r.y + r.h },
      { name: 's', x: mx, y: r.y + r.h },
      { name: 'sw', x: r.x, y: r.y + r.h },
      { name: 'w', x: r.x, y: my }
    ];
  };

  View.prototype.hit = function (sx, sy) {
    var sel = this.selBox();
    if (sel) {
      var r = this.rectToScreen(sel.x0, sel.y0, sel.x1, sel.y1);
      var pts = this.handlePoints(r);
      for (var i = 0; i < pts.length; i++) {
        var h = pts[i];
        if (Math.abs(sx - h.x) <= HS / 2 + 2 && Math.abs(sy - h.y) <= HS / 2 + 2) {
          return { type: 'handle', handle: h.name, box: sel };
        }
      }
    }
    var best = null, bestArea = Infinity;
    for (var k = this.data.boxes.length - 1; k >= 0; k--) {
      var b = this.data.boxes[k];
      var br = this.rectToScreen(b.x0, b.y0, b.x1, b.y1);
      if (sx >= br.x - 1 && sx <= br.x + br.w + 1 && sy >= br.y - 1 && sy <= br.y + br.h + 1) {
        var a = (b.x1 - b.x0) * (b.y1 - b.y0);
        if (a < bestArea) { bestArea = a; best = b; }
      }
    }
    return best ? { type: 'box', box: best } : null;
  };

  View.prototype.ensureVisible = function (b) {
    if (!b || !this.scale) { return; }
    var r = this.rectToScreen(b.x0, b.y0, b.x1, b.y1);
    var pad = 36, bottom = this.ch - 26;
    var dx = 0, dy = 0;
    if (r.w < this.cw - pad * 2) {
      if (r.x < pad) { dx = pad - r.x; }
      else if (r.x + r.w > this.cw - pad) { dx = (this.cw - pad) - (r.x + r.w); }
    }
    if (r.h < bottom - pad * 2) {
      if (r.y < pad) { dy = pad - r.y; }
      else if (r.y + r.h > bottom - pad) { dy = (bottom - pad) - (r.y + r.h); }
    }
    if (dx || dy) { this.tx += dx; this.ty += dy; this.invalidate(); }
  };

  View.prototype._cursor = function (name) {
    if (name) { this.canvas.style.cursor = name; return; }
    if (this.mode === 'col') {
      var idx = this.drag.idx;
      var cols = this.data.columns;
      if (idx > 0 && idx < cols.length) {
        var left = cols[idx - 1], right = cols[idx];
        var nx = clamp(ip.x, left.x0 + 40, right.x1 - 40);
        left.x1 = rnd(nx);
        right.x0 = rnd(nx);
        this.invalidate();
      }
      return;
    }
    if (this.mode === 'pan') { this.canvas.style.cursor = 'grabbing'; return; }
    if (this.space) { this.canvas.style.cursor = 'grab'; return; }
    if (this.armed) { this.canvas.style.cursor = 'crosshair'; return; }
    this.canvas.style.cursor = 'default';
  };

  View.prototype._hoverCursor = function (hit, p) {
    var ci = this._colBoundaryAt(p.x);
    this.hoverCol = ci;
    if (ci >= 0) { this._cursor('col-resize'); this.invalidate(); return; }
    if (this.space) { this._cursor('grab'); return; }
    if (hit && hit.type === 'handle') { this._cursor(CURSORS[hit.handle] || 'default'); return; }
    if (hit && hit.type === 'box') { this._cursor('move'); return; }
    this._cursor(null);
  };

  // ------------------------------------------------------------------ 事件
  View.prototype._bind = function () {
    var self = this;
    this.canvas.addEventListener('pointerdown', function (e) { self._down(e); });
    this.canvas.addEventListener('pointermove', function (e) { self._move(e); });
    this.canvas.addEventListener('pointerup', function (e) { self._up(e); });
    this.canvas.addEventListener('pointercancel', function (e) { self._up(e); });
    this.canvas.addEventListener('contextmenu', function (e) { e.preventDefault(); });
    this.canvas.addEventListener('dblclick', function (e) { e.preventDefault(); self.resetView(); });
    this.canvas.addEventListener('wheel', function (e) { self._wheel(e); }, { passive: false });
    this.canvas.addEventListener('pointerleave', function () {
      self.mouse = null;
      if (self.opts.onMouseMove) { self.opts.onMouseMove(null); }
    });
    window.addEventListener('keydown', function (e) {
      if (e.code === 'Space' && !isTyping(e.target)) { self.space = true; self._cursor(null); }
    }, true);
    window.addEventListener('keyup', function (e) {
      if (e.code === 'Space') { self.space = false; self._cursor(null); }
    }, true);
    window.addEventListener('blur', function () { self.space = false; });
    if (window.ResizeObserver) {
      new ResizeObserver(function () { self._resize(); }).observe(this.wrap);
    } else {
      window.addEventListener('resize', function () { self._resize(); });
    }
  };

  function isTyping(el) {
    if (!el || !el.tagName) { return false; }
    var t = el.tagName.toUpperCase();
    return t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT' || el.isContentEditable;
  }

  View.prototype._wheel = function (ev) {
    if (!this.data.w) { return; }
    ev.preventDefault();
    var p = this.screenPos(ev);
    var k = ev.deltaMode === 1 ? 0.06 : 0.0016;
    var f = Math.exp(-ev.deltaY * k);
    this.setZoomAt(this.zoom * f, p.x, p.y);
  };

  View.prototype._down = function (ev) {
    if (ev.button === 2) { ev.preventDefault(); return; }
    if (ev.button !== 0 && ev.button !== 1) { return; }
    try { this.canvas.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
    var p = this.screenPos(ev);
    var ip = this.imgFromScreen(p.x, p.y);
    this.mouse = { sx: p.x, sy: p.y, x: ip.x, y: ip.y };
    var ci = this._colBoundaryAt(p.x);
    if (ci >= 0 && !this.space) {
      this.mode = 'col';
      this.drag = { idx: ci };
      this.invalidate();
      return;
    }
    if (ev.button === 1 || this.space) {
      this.mode = 'pan';
      this.drag = { sx: p.x, sy: p.y, tx: this.tx, ty: this.ty };
      this._cursor('grabbing');
      ev.preventDefault();
      return;
    }
    if (this.armed) {
      this.mode = 'create';
      this.create = { x0: ip.x, y0: ip.y, x1: ip.x, y1: ip.y };
      this.invalidate();
      return;
    }
    var hit = this.hit(p.x, p.y);
    if (hit && hit.type === 'handle') {
      this.mode = 'resize';
      this.drag = {
        handle: hit.handle, box: hit.box, ip: ip,
        orig: { x0: hit.box.x0, y0: hit.box.y0, x1: hit.box.x1, y1: hit.box.y1 }
      };
      if (this.opts.onEditStart) { this.opts.onEditStart(); }
      return;
    }
    if (hit && hit.type === 'box') {
      if (this.data.selId !== hit.box.id) {
        this.data.selId = hit.box.id;
        if (this.opts.onSelect) { this.opts.onSelect(hit.box.id); }
      }
      this.mode = 'move';
      this.drag = {
        box: hit.box, ip: ip,
        orig: { x0: hit.box.x0, y0: hit.box.y0, x1: hit.box.x1, y1: hit.box.y1 }
      };
      if (this.opts.onEditStart) { this.opts.onEditStart(); }
      return;
    }
    if (this.data.selId !== null) {
      this.data.selId = null;
      if (this.opts.onSelect) { this.opts.onSelect(null); }
    }
    this.mode = 'pan';
    this.drag = { sx: p.x, sy: p.y, tx: this.tx, ty: this.ty };
    this._cursor('grabbing');
    this.invalidate();
  };

  View.prototype._move = function (ev) {
    var p = this.screenPos(ev);
    var ip = this.imgFromScreen(p.x, p.y);
    this.mouse = { sx: p.x, sy: p.y, x: ip.x, y: ip.y };
    if (this.opts.onMouseMove) { this.opts.onMouseMove(this.mouse); }
    var d = this.data;
    if (this.mode === 'idle') {
      var hit = this.hit(p.x, p.y);
      var hov = hit && hit.type === 'box' ? hit.box.id : null;
      if (hov !== this.hoverId) { this.hoverId = hov; this.invalidate(); }
      if (this.armed) { this.invalidate(); }   // 让十字准线跟着指针走
      this._hoverCursor(hit, p);
      return;
    }
    if (this.mode === 'col') {
      var idx = this.drag.idx, cols = this.data.columns;
      if (idx > 0 && idx < cols.length) {
        var left = cols[idx - 1], right = cols[idx];
        var nx = clamp(ip.x, left.x0 + 40, right.x1 - 40);
        left.x1 = rnd(nx); right.x0 = rnd(nx);
        this.invalidate();
      }
      return;
    }
    if (this.mode === 'pan') {
      this.tx = this.drag.tx + (p.x - this.drag.sx);
      this.ty = this.drag.ty + (p.y - this.drag.sy);
      this.invalidate();
      return;
    }
    if (this.mode === 'create') {
      this.create.x1 = ip.x; this.create.y1 = ip.y;
      this.invalidate();
      return;
    }
    if (this.mode === 'move') {
      var b = this.drag.box, o = this.drag.orig;
      var dx = ip.x - this.drag.ip.x, dy = ip.y - this.drag.ip.y;
      dx = clamp(o.x0 + dx, 0, d.w - (o.x1 - o.x0)) - o.x0;
      dy = clamp(o.y0 + dy, 0, d.h - (o.y1 - o.y0)) - o.y0;
      b.x0 = rnd(o.x0 + dx); b.y0 = rnd(o.y0 + dy);
      b.x1 = rnd(o.x1 + dx); b.y1 = rnd(o.y1 + dy);
      if (this.opts.onEditChange) { this.opts.onEditChange(); }
      this.invalidate();
      return;
    }
    if (this.mode === 'resize') {
      var bx = this.drag.box, or = this.drag.orig, h = this.drag.handle;
      var nx0 = or.x0, ny0 = or.y0, nx1 = or.x1, ny1 = or.y1;
      if (h.indexOf('w') >= 0) { nx0 = clamp(ip.x, 0, or.x1 - MIN_SIZE); }
      if (h.indexOf('e') >= 0) { nx1 = clamp(ip.x, or.x0 + MIN_SIZE, d.w); }
      if (h.indexOf('n') >= 0) { ny0 = clamp(ip.y, 0, or.y1 - MIN_SIZE); }
      if (h.indexOf('s') >= 0) { ny1 = clamp(ip.y, or.y0 + MIN_SIZE, d.h); }
      bx.x0 = rnd(nx0); bx.y0 = rnd(ny0); bx.x1 = rnd(nx1); bx.y1 = rnd(ny1);
      if (this.opts.onEditChange) { this.opts.onEditChange(); }
      this.invalidate();
    }
  };

  View.prototype._up = function (ev) {
    var was = this.mode;
    if (was === 'col') {
      this.mode = 'idle';
      this.drag = null;
      if (this.opts.onColumnsChange) {
        this.opts.onColumnsChange(this.data.columns.map(function (c) { return { x0: c.x0, x1: c.x1 }; }));
      }
      this.invalidate();
      return;
    }
    if (was === 'create' && this.create) {
      var c = this.create;
      var x0 = Math.min(c.x0, c.x1), x1 = Math.max(c.x0, c.x1);
      var y0 = Math.min(c.y0, c.y1), y1 = Math.max(c.y0, c.y1);
      this.create = null;
      if (x1 - x0 >= MIN_SIZE && y1 - y0 >= MIN_SIZE) {
        if (this.opts.onCreate) {
          this.opts.onCreate({ x0: rnd(x0), y0: rnd(y0), x1: rnd(x1), y1: rnd(y1) });
        }
      }
    } else if (was === 'move' || was === 'resize') {
      if (this.opts.onEditEnd) { this.opts.onEditEnd(); }
    }
    this.mode = 'idle';
    this.drag = null;
    this._cursor(null);
    this.invalidate();
  };

  // ------------------------------------------------------------------ 绘制
  View.prototype.render = function () {
    var ctx = this.ctx, d = this.data, i, b, r;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = '#0b0e13';
    ctx.fillRect(0, 0, this.cw, this.ch);
    if (this.img && this.imgOk && d.w && d.h) {
      r = this.rectToScreen(0, 0, d.w, d.h);
      ctx.save();
      ctx.shadowColor = 'rgba(0,0,0,0.55)';
      ctx.shadowBlur = 18;
      ctx.shadowOffsetY = 4;
      ctx.fillStyle = '#ded6c4';
      ctx.fillRect(r.x, r.y, r.w, r.h);
      ctx.restore();
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(this.img, r.x, r.y, r.w, r.h);
      this._drawColumns(r);
      ctx.strokeStyle = 'rgba(148,163,184,0.35)';
      ctx.lineWidth = 1;
      ctx.strokeRect(rnd(r.x) + 0.5, rnd(r.y) + 0.5, rnd(r.w), rnd(r.h));
    } else {
      this._drawPlaceholder();
    }
    this._drawColumns();
    this._drawGuides();
    this._drawBoxes();
    if (this.mode === 'create' && this.create) {
      var c = this.create;
      var cr = this.rectToScreen(c.x0, c.y0, c.x1, c.y1);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = '#ef4444';
      ctx.lineWidth = 1.5;
      ctx.fillStyle = 'rgba(239,68,68,0.10)';
      ctx.fillRect(cr.x, cr.y, cr.w, cr.h);
      ctx.strokeRect(rnd(cr.x) + 0.5, rnd(cr.y) + 0.5, rnd(cr.w), rnd(cr.h));
      ctx.restore();
    }
  };

  View.prototype._drawPlaceholder = function () {
    var ctx = this.ctx;
    if (this.imgErr) {
      ctx.fillStyle = '#8b98a9';
      ctx.font = '13px Segoe UI, Microsoft YaHei, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText('预览图不可用（该页可能尚未处理）', this.cw / 2, this.ch / 2);
      ctx.textAlign = 'left';
    }
  };

  View.prototype._drawColumns = function (r) {
    var d = this.data;
    if (!d.columns || !d.columns.length) { return; }
    var ctx = this.ctx;
    ctx.save();
    ctx.strokeStyle = 'rgba(56,189,248,0.16)';
    ctx.lineWidth = 1;
    for (var i = 0; i < d.columns.length; i++) {
      var col = d.columns[i];
      var sx = col.x0 * this.scale + this.tx;
      ctx.beginPath();
      ctx.moveTo(rnd(sx) + 0.5, r.y);
      ctx.lineTo(rnd(sx) + 0.5, r.y + r.h);
      ctx.stroke();
    }
    ctx.restore();
  };

  // ---- 分列线：可拖动调整，用户手动改过就以手动为准
  View.prototype.setShowColumns = function (on) {
    this.showCols = !!on;
    this.invalidate();
  };

  View.prototype._colBounds = function () {
    var cols = this.data.columns || [];
    var out = [];
    for (var i = 0; i < cols.length; i++) {
      var c = cols[i];
      if (i === 0) { out.push(c.x0); }
      out.push(c.x1);
    }
    return out;
  };

  View.prototype._colBoundaryAt = function (sx) {
    if (!this.showCols) { return -1; }
    var bs = this._colBounds();
    for (var i = 0; i < bs.length; i++) {
      var px = bs[i] * this.scale + this.tx;
      if (Math.abs(px - sx) <= 6) { return i; }
    }
    return -1;
  };

  View.prototype._drawColumns = function () {
    var cols = this.data.columns || [];
    if (!this.showCols || !cols.length) { return; }
    var ctx = this.ctx;
    var bs = this._colBounds();
    ctx.save();
    ctx.setLineDash([5, 5]);
    ctx.lineWidth = 1;
    for (var i = 0; i < bs.length; i++) {
      var x = Math.round(bs[i] * this.scale + this.tx) + 0.5;
      if (x < -2 || x > this.cw + 2) { continue; }
      ctx.strokeStyle = (i === this.hoverCol) ? 'rgba(250,204,21,0.95)' : 'rgba(96,165,250,0.55)';
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, this.ch);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // 顶部小把手，提示可以拖
    for (var k = 0; k < bs.length; k++) {
      var hx = Math.round(bs[k] * this.scale + this.tx);
      if (hx < -6 || hx > this.cw + 6) { continue; }
      ctx.fillStyle = (k === this.hoverCol) ? 'rgba(250,204,21,0.95)' : 'rgba(96,165,250,0.75)';
      ctx.fillRect(hx - 4, 0, 8, 12);
      ctx.fillStyle = '#0b1220';
      ctx.fillRect(hx - 1, 3, 2, 6);
    }
    ctx.restore();
  };

  // ---- 框编号：按「最左列起、列内从上到下」动态编号（与导出 index.csv 的 visual 顺序一致）
  View.prototype._renumber = function () {
    var boxes = this.data.boxes || [];
    var cols = this.data.columns || [];
    var centers = [];
    for (var i = 0; i < cols.length; i++) {
      var c0 = cols[i];
      var x0 = parseFloat(c0.x0), x1 = parseFloat(c0.x1);
      if (isFinite(x0) && isFinite(x1)) { centers.push((x0 + x1) / 2); }
    }
    function colCx(b) {
      var cx = (b.x0 + b.x1) / 2, best = null, bestD = null;
      for (var k = 0; k < centers.length; k++) {
        var dd = Math.abs(centers[k] - cx);
        if (bestD === null || dd < bestD) { bestD = dd; best = centers[k]; }
      }
      return best === null ? cx : best;
    }
    var rows = boxes.map(function (b) { return { b: b, kx: colCx(b) }; });
    rows.sort(function (p, q) {
      if (p.kx !== q.kx) { return p.kx - q.kx; }
      if (p.b.y0 !== q.b.y0) { return p.b.y0 - q.b.y0; }
      return p.b.x0 - q.b.x0;
    });
    var map = {};
    for (var m = 0; m < rows.length; m++) { map[rows[m].b.id] = m + 1; }
    this.numMap = map;
    this.numCount = rows.length;
    this.numDigits = Math.max(3, String(Math.max(1, rows.length)).length);
  };

  View.prototype.numOf = function (id) { return (this.numMap && this.numMap[id]) || 0; };

  View.prototype._numText = function (n) {
    var s = String(n);
    while (s.length < this.numDigits) { s = '0' + s; }
    return s;
  };

  View.prototype._drawBoxes = function () {
    var ctx = this.ctx, d = this.data, i, b, r, sel;
    ctx.textAlign = 'left';
    ctx.font = '11px Consolas, ui-monospace, monospace';
    for (i = 0; i < d.boxes.length; i++) {
      b = d.boxes[i];
      r = this.rectToScreen(b.x0, b.y0, b.x1, b.y1);
      if (r.x + r.w < -4 || r.y + r.h < -4 || r.x > this.cw + 4 || r.y > this.ch + 4) { continue; }
      sel = b.id === d.selId;
      // 全部实线、常驻：人工自选色 > 已确认/存疑 > 规范字匹配等级
      var lvl = b.refLevel || '';
      var manual = b.color || '';
      var col = sel ? '#ef4444'
        : (manual ? manual
          : (b.review === 1 ? '#22c55e'
            : (b.review === 2 ? '#f59e0b'
              : (lvl === 'strong' ? '#22c55e' : (lvl === 'low' ? '#f87171' : '#22d3ee')))));
      if (sel) {
        ctx.fillStyle = 'rgba(239,68,68,0.10)';
        ctx.fillRect(r.x, r.y, r.w, r.h);
      } else if (this.hoverId === b.id) {
        ctx.fillStyle = 'rgba(226,232,240,0.10)';
        ctx.fillRect(r.x, r.y, r.w, r.h);
      }
      ctx.globalAlpha = (sel || b.review !== 0) ? 1 : 0.9;
      ctx.strokeStyle = col;
      ctx.setLineDash([]);
      ctx.lineWidth = sel ? 2 : (this.hoverId === b.id ? 2 : 1.25);
      ctx.strokeRect(rnd(r.x) + 0.5, rnd(r.y) + 0.5, rnd(r.w), rnd(r.h));
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;
      // 编号角标：所有框都显示，增删框后自动重排
      var num = this.numOf(b.id);
      // 太小的框（整页适配视图）不画编号，避免糊成一片；放大或选中时显示
      if (num && (sel || (r.w >= 30 && r.h >= 22))) {
        var txt = this._numText(num);
        ctx.font = '600 10px Consolas, ui-monospace, monospace';
        var tw = ctx.measureText(txt).width;
        var ly = r.y - 4;
        if (ly < 12) { ly = r.y + 12; }
        var bw = tw + 7, bh = 13;
        ctx.fillStyle = sel ? 'rgba(239,68,68,0.92)' : 'rgba(2,6,23,0.72)';
        ctx.beginPath();
        if (ctx.roundRect) { ctx.roundRect(r.x - 1, ly - 10, bw, bh, 3); ctx.fill(); }
        else { ctx.fillRect(r.x - 1, ly - 10, bw, bh); }
        ctx.fillStyle = sel ? '#ffffff' : col;
        ctx.fillText(txt, r.x + 2.5, ly);
      }
    }
    var sb = this.selBox();
    if (sb) {
      var sr = this.rectToScreen(sb.x0, sb.y0, sb.x1, sb.y1);
      var pts = this.handlePoints(sr);
      for (i = 0; i < pts.length; i++) {
        var h = pts[i];
        ctx.fillStyle = '#ffffff';
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.rect(rnd(h.x - HS / 2) + 0.5, rnd(h.y - HS / 2) + 0.5, HS, HS);
        ctx.fill();
        ctx.stroke();
      }
    }
  };

  window.YI = window.YI || {};
  window.YI.CanvasView = View;
  window.YI.HANDLES = HANDLES;
})();