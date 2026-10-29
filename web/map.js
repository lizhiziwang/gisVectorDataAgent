/* 轻量矢量地图渲染器：Canvas 2D + Web Mercator，无外部依赖、可离线运行。
   支持：平移/滚轮缩放/双击放大/框选缩放、多边形+线+点符号化、悬停高亮、点击拾取、经纬网、比例尺。 */
(function () {
  const D2R = Math.PI / 180;

  function project(lon, lat) {
    const x = (lon + 180) / 360;
    const s = Math.sin(Math.max(-85.05, Math.min(85.05, lat)) * D2R);
    const y = 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
    return { x, y };
  }

  function unproject(x, y) {
    const lon = x * 360 - 180;
    const n = Math.PI - 2 * Math.PI * y;
    const lat = (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n)));
    return { lon, lat };
  }

  class VMap {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext('2d');
      this.layers = [];          // [{id,name,color,visible,geojson}]
      this.scale = 4000;         // px per world unit
      this.tx = 0;
      this.ty = 0;
      this.hover = null;
      this.hoverLayer = null;
      this.paths = [];           // 屏幕空间命中索引
      this.onHover = null;
      this.onPick = null;
      this.onView = null;
      this._raf = null;
      this.visible = { grid: true, labels: true };
      this._bind();
      this.resize();
      this.loop = this.loop.bind(this);
      // 容器尺寸变化（窗口缩放、面板折叠）时自动跟随，避免画布尺寸与布局不一致
      if (window.ResizeObserver) {
        this._ro = new ResizeObserver(() => {
          const r = this.canvas.parentElement.getBoundingClientRect();
          if (Math.abs(r.width - this.w) > 1 || Math.abs(r.height - this.h) > 1) this.resize();
        });
        this._ro.observe(this.canvas.parentElement);
      }
    }

    /* ------------------------------ 视图 ------------------------------ */
    resize() {
      const dpr = window.devicePixelRatio || 1;
      const r = this.canvas.parentElement.getBoundingClientRect();
      const prevW = this.w, prevH = this.h;
      const center = (prevW && prevH) ? this.screenToLonLat(prevW / 2, prevH / 2) : null;
      this.w = Math.max(r.width, 10);
      this.h = Math.max(r.height, 10);
      this.canvas.width = this.w * dpr;
      this.canvas.height = this.h * dpr;
      this.canvas.style.width = this.w + 'px';
      this.canvas.style.height = this.h + 'px';
      this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (!this._inited) {
        this._inited = true;
        if (!this.layers.length) { this.fitBounds([74, 18, 135.5, 53.5], 0.02); return; }
      } else if (center && prevW !== this.w) {
        const p = project(center.lon, center.lat);
        this.tx = this.w / 2 - p.x * this.scale;
        this.ty = this.h / 2 - p.y * this.scale;
      }
      if (!this.layers.length) { this.fitBounds([74, 18, 135.5, 53.5], 0.02); return; }
      this.draw();
    }

    setLayers(layers) {
      const hadData = this.layers.length > 0;
      // 面积覆盖型结果（缓冲/融合/网格/合并）先画，原始与细粒度图层叠在上面，避免糊成一片
      const back = new Set(['buffer', 'dissolve', 'grid', 'merge', 'convex_hull', 'voronoi']);
      const rank = l => (back.has(l.op) ? 0 : 1);
      this.layers = layers.map((l, i) => ({ l, i }))
        .sort((a, b) => (rank(a.l) - rank(b.l)) || (a.i - b.i))
        .map(x => x.l);
      if (!hadData && layers.length) this.fitAll();
      this.draw();
    }

    toScreen(w) {
      return { x: w.x * this.scale + this.tx, y: w.y * this.scale + this.ty };
    }

    toWorld(p) {
      return { x: (p.x - this.tx) / this.scale, y: (p.y - this.ty) / this.scale };
    }

    lonlatToScreen(lon, lat) {
      return this.toScreen(project(lon, lat));
    }

    screenToLonLat(x, y) {
      return unproject(...Object.values(this.toWorld({ x, y })));
    }

    bounds() {
      let b = null;
      for (const l of this.layers) {
        if (!l.visible || !l.geojson) continue;
        const bb = extentOf(l.geojson);
        if (!bb) continue;
        b = b ? [Math.min(b[0], bb[0]), Math.min(b[1], bb[1]), Math.max(b[2], bb[2]), Math.max(b[3], bb[3])] : bb;
      }
      return b;
    }

    fitAll(pad = 0.12) {
      const b = this.bounds();
      if (b) this.fitBounds(b, pad);
    }

    fitBounds(b, pad = 0.12) {
      const p1 = project(b[0], b[3]);
      const p2 = project(b[2], b[1]);
      const bw = Math.max(Math.abs(p2.x - p1.x), 1e-6);
      const bh = Math.max(Math.abs(p2.y - p1.y), 1e-6);
      const s = Math.min(this.w / bw, this.h / bh) * (1 - pad * 2);
      this.scale = Math.min(Math.max(s, 60), 4e7);
      const cx = (p1.x + p2.x) / 2, cy = (p1.y + p2.y) / 2;
      this.tx = this.w / 2 - cx * this.scale;
      this.ty = this.h / 2 - cy * this.scale;
      this.draw();
      this.emitView();
    }

    fitLayer(id) {
      const l = this.layers.find(x => x.id === id);
      if (!l || !l.geojson) return;
      const b = extentOf(l.geojson);
      if (b) this.fitBounds(b, 0.15);
    }

    zoom(factor, cx, cy) {
      const px = cx ?? this.w / 2, py = cy ?? this.h / 2;
      const before = this.toWorld({ x: px, y: py });
      this.scale = Math.min(Math.max(this.scale * factor, 40), 6e7);
      const after = this.toWorld({ x: px, y: py });
      this.tx += (after.x - before.x) * this.scale;
      this.ty += (after.y - before.y) * this.scale;
      this.draw();
      this.emitView();
    }

    zoomBy(dz) {
      this.zoom(Math.pow(2, dz));
    }

    emitView() {
      if (this.onView) {
        const c = this.screenToLonLat(this.w / 2, this.h / 2);
        this.onView({ lon: c.lon, lat: c.lat, zoom: Math.log2(this.scale / 256) });
      }
    }

    mpp() {
      const c = this.screenToLonLat(this.w / 2, this.h / 2);
      return 156543.03392 * Math.cos(c.lat * D2R) / (this.scale / 256);
    }

    /* ------------------------------ 交互 ------------------------------ */
    _bind() {
      const cv = this.canvas;
      let dragging = false, sx = 0, sy = 0, moved = 0, shiftBox = null;
      cv.addEventListener('mousedown', e => {
        if (e.button !== 0) return;
        if (e.shiftKey) { shiftBox = { x: e.offsetX, y: e.offsetY }; return; }
        dragging = true; moved = 0; sx = e.offsetX; sy = e.offsetY; cv.style.cursor = 'grabbing';
      });
      window.addEventListener('mouseup', e => {
        if (shiftBox) {
          const x = e.offsetX ?? 0, y = e.offsetY ?? 0;
          // 仅在画布内完成框选
          if (this._boxEnd) { this._zoomToBox(this._boxStart, this._boxEnd); }
          shiftBox = null; this._boxStart = null; this._boxEnd = null; this.draw();
        }
        if (dragging && moved < 4) { this._pick(e); }
        dragging = false; cv.style.cursor = 'grab';
      });
      cv.addEventListener('mousemove', e => {
        if (shiftBox) {
          this._boxStart = shiftBox; this._boxEnd = { x: e.offsetX, y: e.offsetY };
          this.draw();
          return;
        }
        if (dragging) {
          this.tx += e.movementX; this.ty += e.movementY;
          moved += Math.abs(e.movementX) + Math.abs(e.movementY);
          this.draw(); this.emitView();
        } else {
          this._hoverAt(e.offsetX, e.offsetY);
        }
        if (this.onCursor) {
          const ll = this.screenToLonLat(e.offsetX, e.offsetY);
          this.onCursor({ lon: ll.lon, lat: ll.lat, x: e.offsetX, y: e.offsetY });
        }
      });
      cv.addEventListener('mouseleave', () => { if (this.hover) { this.hover = null; this.draw(); if (this.onHover) this.onHover(null); } });
      cv.addEventListener('wheel', e => {
        e.preventDefault();
        const f = Math.pow(1.0016, -e.deltaY);
        this.zoom(f, e.offsetX, e.offsetY);
        this._hoverAt(e.offsetX, e.offsetY);
      }, { passive: false });
      cv.addEventListener('dblclick', e => { this.zoom(2, e.offsetX, e.offsetY); });
      cv.style.cursor = 'grab';
    }

    _zoomToBox(a, b) {
      if (!a || !b) return;
      const w1 = this.toWorld(a), w2 = this.toWorld(b);
      const bw = Math.abs(w2.x - w1.x), bh = Math.abs(w2.y - w1.y);
      if (bw < 3 / this.scale || bh < 3 / this.scale) return;
      this.scale = Math.min(this.w / bw, this.h / bh);
      this.tx = this.w / 2 - ((w1.x + w2.x) / 2) * this.scale;
      this.ty = this.h / 2 - ((w1.y + w2.y) / 2) * this.scale;
      this.emitView();
    }

    _hit(x, y) {
      const ctx = this.ctx;
      for (let i = this.paths.length - 1; i >= 0; i--) {
        const p = this.paths[i];
        if (p.kind === 'point') {
          const dx = x - p.cx, dy = y - p.cy;
          if (dx * dx + dy * dy <= 49) return p;
        } else if (p.kind === 'line') {
          ctx.lineWidth = 7;
          if (ctx.isPointInStroke(p.path, x, y)) return p;
        } else {
          if (ctx.isPointInPath(p.path, x, y)) return p;
        }
      }
      return null;
    }

    _hoverAt(x, y) {
      const p = this._hit(x, y);
      const changed = (p && p.f) !== (this.hover && this.hover.f);
      this.hover = p;
      if (changed) {
        this.draw();
        if (this.onHover) this.onHover(p ? { props: p.f.properties, layer: p.layer, x, y } : null);
      }
    }

    _pick(e) {
      const p = this._hit(e.offsetX, e.offsetY);
      if (this.onPick) this.onPick(p ? { props: p.f.properties, layer: p.layer, x: e.offsetX, y: e.offsetY } : null);
    }

    /* ------------------------------ 渲染 ------------------------------ */
    schedule() {
      if (this._raf) return;
      this._raf = requestAnimationFrame(() => { this._raf = null; this.draw(); });
    }

    loop() { this.draw(); }

    draw() {
      const ctx = this.ctx;
      if (!ctx) return;
      ctx.clearRect(0, 0, this.w, this.h);
      // 背景
      const g = ctx.createLinearGradient(0, 0, 0, this.h);
      g.addColorStop(0, '#fbfcfe');
      g.addColorStop(1, '#f2f5fa');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, this.w, this.h);

      this.paths = [];
      if (this.visible.grid) this._grid(ctx);

      for (const layer of this.layers) {
        if (!layer.visible || !layer.geojson) continue;
        this._drawLayer(ctx, layer);
      }

      if (this._boxStart && this._boxEnd) {
        ctx.save();
        ctx.fillStyle = 'rgba(47,111,237,0.12)';
        ctx.strokeStyle = '#2f6fed';
        ctx.lineWidth = 1;
        const x = Math.min(this._boxStart.x, this._boxEnd.x), y = Math.min(this._boxStart.y, this._boxEnd.y);
        const w = Math.abs(this._boxEnd.x - this._boxStart.x), h = Math.abs(this._boxEnd.y - this._boxStart.y);
        ctx.fillRect(x, y, w, h); ctx.strokeRect(x + .5, y + .5, w, h);
        ctx.restore();
      }
    }

    _grid(ctx) {
      const steps = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30];
      let step = steps[steps.length - 1];
      // step 单位为度，换算成像素需 /360（世界坐标系 1 = 360°）
      for (const s of steps) { if (s / 360 * this.scale > 90) { step = s; break; } }
      const tl = this.screenToLonLat(0, 0), br = this.screenToLonLat(this.w, this.h);
      ctx.save();
      ctx.strokeStyle = 'rgba(120,140,170,0.16)';
      ctx.lineWidth = 1;
      const dec = step >= 1 ? 0 : step >= 0.1 ? 1 : step >= 0.01 ? 2 : 3;
      const fmtDeg = (v, pos, neg) => (Math.abs(v) < 1e-9 ? '0°' : Math.abs(v).toFixed(dec) + '°' + (v > 0 ? pos : neg));
      ctx.fillStyle = 'rgba(110,128,152,0.75)';
      const x0 = Math.floor(tl.lon / step) * step, x1 = br.lon;
      for (let lon = x0; lon <= x1 + step; lon += step) {
        const sx = this.lonlatToScreen(lon, 0).x;
        ctx.beginPath(); ctx.moveTo(Math.round(sx) + .5, 0); ctx.lineTo(Math.round(sx) + .5, this.h); ctx.stroke();
        if (this.visible.labels) {
          const txt = fmtDeg(lon, 'E', 'W');
          ctx.fillText(txt, sx + 4, this.h - 6);
        }
      }
      const yTop = Math.max(tl.lat, br.lat), yBot = Math.min(tl.lat, br.lat);
      const y0 = Math.floor(yBot / step) * step;
      for (let lat = y0; lat <= yTop + step; lat += step) {
        const sy = this.lonlatToScreen(0, lat).y;
        ctx.beginPath(); ctx.moveTo(0, Math.round(sy) + .5); ctx.lineTo(this.w, Math.round(sy) + .5); ctx.stroke();
        if (this.visible.labels) {
          const txt = fmtDeg(lat, 'N', 'S');
          ctx.fillText(txt, 6, sy - 4);
        }
      }
      ctx.restore();
    }

    _drawLayer(ctx, layer) {
      const color = layer.color || '#2f6fed';
      const feats = (layer.geojson && layer.geojson.features) || [];
      const polys = [], lines = [], pts = [];
      for (const f of feats) {
        const t = f.geometry && f.geometry.type;
        if (!t) continue;
        if (t.includes('Polygon')) polys.push(f);
        else if (t.includes('Line')) lines.push(f);
        else pts.push(f);
      }
      // 面（缓冲区/网格等大面积重叠图层用更淡的填充，避免糊成一片）
      const soft = layer.op === 'buffer' || layer.op === 'grid' || layer.op === 'merge' || layer.op === 'voronoi';
      const fillA = soft ? 0.055 : layer.op === 'dissolve' ? 0.08 : 0.14;
      const strokeA = soft ? 0.32 : 0.7;
      for (const f of polys) {
        const path = this._pathOfGeometry(f.geometry);
        if (!path) continue;
        ctx.fillStyle = hexA(color, fillA);
        ctx.strokeStyle = hexA(color, strokeA);
        ctx.lineWidth = soft ? 0.6 : 0.9;
        ctx.fill(path, 'evenodd');
        ctx.stroke(path);
        this.paths.push({ kind: 'poly', path, f, layer });
      }
      // 线
      for (const f of lines) {
        const path = this._pathOfGeometry(f.geometry);
        if (!path) continue;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.9;
        ctx.lineCap = 'round';
        ctx.stroke(path);
        this.paths.push({ kind: 'line', path, f, layer });
      }
      // 点
      ctx.save();
      for (const f of pts) {
        const c = this._centerOf(f.geometry);
        if (!c) continue;
        const p = this.toScreen(project(c[0], c[1]));
        ctx.beginPath();
        ctx.arc(p.x, p.y, 4.2, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = '#fff';
        ctx.stroke();
        this.paths.push({ kind: 'point', cx: p.x, cy: p.y, f, layer });
      }
      ctx.restore();

      // 悬停高亮
      if (this.hover && this.hover.layer && this.hover.layer.id === layer.id) {
        const h = this.hover;
        ctx.save();
        if (h.kind === 'point') {
          ctx.beginPath(); ctx.arc(h.cx, h.cy, 7.5, 0, Math.PI * 2);
          ctx.fillStyle = 'rgba(255,255,255,0.9)'; ctx.fill();
          ctx.strokeStyle = '#111827'; ctx.lineWidth = 2; ctx.stroke();
        } else {
          ctx.strokeStyle = '#111827';
          ctx.lineWidth = 2.4;
          ctx.stroke(h.path);
        }
        ctx.restore();
      }
    }

    _centerOf(geom) {
      if (!geom) return null;
      if (geom.type === 'Point') return geom.coordinates;
      const cs = firstCoord(geom.coordinates);
      return cs;
    }

    _pathOfGeometry(geom) {
      const path = new Path2D();
      const body = this._bodyCoords(geom);
      if (!body) return null;
      for (const ring of body) {
        if (!ring.length) continue;
        const p0 = this.toScreen(project(ring[0][0], ring[0][1]));
        path.moveTo(p0.x, p0.y);
        for (let i = 1; i < ring.length; i++) {
          const p = this.toScreen(project(ring[i][0], ring[i][1]));
          path.lineTo(p.x, p.y);
        }
        path.closePath();
      }
      return path;
    }

    _bodyCoords(geom) {
      const t = geom.type;
      if (t === 'Polygon') return [geom.coordinates[0], ...geom.coordinates.slice(1)];
      if (t === 'MultiPolygon') {
        const out = [];
        for (const poly of geom.coordinates) { out.push(poly[0]); out.push(...poly.slice(1)); }
        return out;
      }
      if (t === 'LineString') return [geom.coordinates];
      if (t === 'MultiLineString') return geom.coordinates;
      return null;
    }
  }

  function firstCoord(coords) {
    if (typeof coords[0] === 'number') return coords;
    if (!coords.length) return null;
    return firstCoord(coords[0]);
  }

  function extentOf(fc) {
    let b = null;
    for (const f of fc.features || []) {
      walk(f.geometry && f.geometry.coordinates);
    }
    function walk(c) {
      if (!c) return;
      if (typeof c[0] === 'number') {
        if (!b) b = [c[0], c[1], c[0], c[1]];
        else {
          b[0] = Math.min(b[0], c[0]); b[1] = Math.min(b[1], c[1]);
          b[2] = Math.max(b[2], c[0]); b[3] = Math.max(b[3], c[1]);
        }
      } else { for (const x of c) walk(x); }
    }
    return b;
  }

  function hexA(hex, a) {
    const h = hex.replace('#', '');
    const n = parseInt(h.length === 3 ? h.split('').map(c => c + c).join('') : h, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }

  window.VMap = VMap;
  window.mapUtil = { project, unproject, extentOf };
})();
