/* 应用主逻辑：图层管理 / 地图联动 / Agent 对话渲染 */
(() => {
  const $ = s => document.querySelector(s);
  const state = { layers: [], current: null, geo: {}, busy: false, planner: 'rule', visible: {} };

  const LABELS = {
    distance_m: '缓冲距离', features_in: '输入要素', features_out: '输出要素',
    tolerance_m: '容差', node_reduction_pct: '节点减少', cells: '网格数', points: '点数',
    mean_distance_m: '平均距离', removed: '移除要素', repaired: '修复几何',
    empty_removed: '清理空几何', from: '原坐标系', to: '目标坐标系',
    expression: '表达式', predicate: '空间关系', total: '合计', area_m2: '总面积',
    length_m: '总长度', meta: '概况', table: '表格', unit: '单位'
  };

  const TEMPLATES = {
    describe: '检查「{L}」的数据质量',
    buffer: '对「{L}」做 500 米缓冲区',
    clip: '用「{O}」裁剪「{L}」',
    intersect: '「{L}」与「{O}」求交',
    union: '「{L}」与「{O}」叠加合并',
    erase: '从「{L}」中擦除「{O}」范围',
    dissolve: '按字段融合「{L}」',
    reproject: '把「{L}」投影到 EPSG:4547',
    simplify: '简化「{L}」容差 10 米',
    centroid: '取「{L}」的质心',
    convex_hull: '生成「{L}」的凸包',
    voronoi: '对「{L}」生成泰森多边形',
    grid: '在「{L}」范围生成 1 公里渔网',
    sjoin: '把「{O}」的属性空间连接到「{L}」',
    nearest: '为「{L}」查找最近的「{O}」',
    filter: '筛选「{L}」中面积大于 10000 的要素',
    field_calc: '在「{L}」新增字段 密度 = 人口 / 面积',
    area_stats: '按用地类型统计「{L}」的面积',
    merge: '合并所有图层',
    dedupe: '对「{L}」去重',
    fix_geometry: '修复「{L}」的无效几何',
    explode: '拆分「{L}」的多部件'
  };

  /* ------------------------------ 基础 ------------------------------ */
  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || JSON.stringify(j); } catch (e) { }
      throw new Error(detail);
    }
    return res.json();
  }

  function toast(msg, isErr) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(t._h);
    t._h = setTimeout(() => { t.className = 'toast'; }, 3200);
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function fmtNum(v) {
    if (v === null || v === undefined) return '—';
    if (typeof v === 'number') {
      if (Number.isInteger(v)) return v.toLocaleString('zh-CN');
      if (Math.abs(v) >= 1000) return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
      return Number(v.toFixed(2)).toString();
    }
    return String(v);
  }

  function fmtMeters(m) {
    if (m === null || m === undefined) return '—';
    if (m >= 1000) return (m / 1000).toFixed(m >= 10000 ? 0 : 2) + ' km';
    return m.toFixed(m < 10 ? 1 : 0) + ' m';
  }

  /* ------------------------------ 地图 ------------------------------ */
  const map = new VMap($('#map'));

  map.onView = v => {
    const mpp = map.mpp();
    $('#stScale').textContent = '比例尺 ≈ ' + fmtMeters(mpp * 120) + ' / 120px';
    $('#mapInfo').textContent = `缩放 ${v.zoom.toFixed(1)} · ${mpp < 1 ? mpp.toFixed(2) + ' m/px' : mpp.toFixed(0) + ' m/px'}`;
  };

  map.onCursor = c => {
    $('#stCursor').textContent = `光标 ${c.lon.toFixed(5)}°E, ${c.lat.toFixed(5)}°N`;
    if (!state.hovering) return;
  };

  map.onHover = (info) => {
    const tip = $('#tooltip');
    state.hovering = !!info;
    if (!info) { tip.classList.remove('show'); return; }
    tip.innerHTML = hoverHtml(info);
    tip.style.left = Math.min(info.x, map.w - 250) + 'px';
    tip.style.top = Math.min(info.y, map.h - 120) + 'px';
    tip.classList.add('show');
  };

  map.onPick = (info) => {
    if (!info) return;
    const tip = $('#tooltip');
    tip.innerHTML = pickHtml(info);
    tip.style.left = Math.min(info.x, map.w - 280) + 'px';
    tip.style.top = Math.min(info.y, map.h - 200) + 'px';
    tip.classList.add('show');
    setTimeout(() => tip.classList.remove('show'), 4200);
  };

  function hoverHtml(info) {
    const p = info.props || {};
    const keys = Object.keys(p).slice(0, 3);
    const lines = keys.map(k => `<b>${esc(k)}</b> ${esc(String(p[k]).slice(0, 40))}`).join('<br>');
    return `<div style="opacity:.65;margin-bottom:2px">${esc(info.layer.name)}</div>${lines || '—'}`;
  }

  function pickHtml(info) {
    const p = info.props || {};
    const rows = Object.keys(p).map(k =>
      `<tr><td style="color:#9fc2ff;padding:1px 8px 1px 0;white-space:nowrap">${esc(k)}</td>
       <td style="padding:1px 0">${esc(String(p[k] ?? '').slice(0, 60))}</td></tr>`).join('');
    return `<div style="font-weight:600;margin-bottom:4px">${esc(info.layer.name)} 要素属性</div>
      <table style="font-size:11px">${rows}</table>`;
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /* ------------------------------ 图层同步 ------------------------------ */
  async function ensureGeo(layer) {
    if (state.geo[layer.id]) return state.geo[layer.id];
    const gj = await api(`/api/layers/${layer.id}/geojson`);
    state.geo[layer.id] = gj;
    return gj;
  }

  async function syncLayers(list, opts = {}) {
    state.layers = list;
    if (!state.current && list.length) state.current = list[list.length - 1].id;
    if (state.current && !list.find(l => l.id === state.current)) state.current = list.length ? list[list.length - 1].id : null;
    for (const l of list) {
      if (state.visible[l.id] === undefined) state.visible[l.id] = true;
      try { await ensureGeo(l); } catch (e) { console.warn(e); }
    }
    const mapLayers = list.map(l => ({
      id: l.id, name: l.name, color: l.color, op: l.op,
      visible: state.visible[l.id] !== false,
      geojson: state.geo[l.id]
    }));
    map.setLayers(mapLayers);
    renderLayers();
    renderLegend();
    $('#layerCount').textContent = list.length;
    $('#emptyHint').style.display = list.length ? 'none' : 'grid';
    const total = list.reduce((a, l) => a + l.count, 0);
    $('#stFeatures').textContent = `要素 ${total.toLocaleString('zh-CN')}`;
    if (opts.fit) {
      map.fitAll();
      // 布局稳定后再校正一次视野，避免首屏按未完成的布局计算范围
      setTimeout(() => map.fitAll(), 160);
    }
  }

  function renderLayers() {
    const box = $('#layerList');
    box.innerHTML = '';
    if (!state.layers.length) {
      box.appendChild(el('div', 'dz-sub', '暂无图层'));
      return;
    }
    for (const l of [...state.layers].reverse()) {
      const item = el('div', 'layer-item' + (l.id === state.current ? ' active' : ''));
      const sw = el('div', 'swatch');
      sw.style.background = l.color;
      const main = el('div', 'li-main');
      main.appendChild(el('div', 'li-name', l.name));
      const meta = el('div', 'li-meta');
      meta.innerHTML = `${l.count.toLocaleString('zh-CN')} 要素 <i>·</i> ${esc(l.geomType)} <i>·</i> ${esc(l.crs)}`;
      main.appendChild(meta);
      const acts = el('div', 'li-actions');
      const eye = el('button', 'mini' + (state.visible[l.id] === false ? ' off' : ''), state.visible[l.id] === false ? '🚫' : '👁');
      eye.title = '显示/隐藏';
      eye.onclick = e => {
        e.stopPropagation();
        state.visible[l.id] = state.visible[l.id] === false;
        map.setLayers(state.layers.map(x => ({ id: x.id, name: x.name, color: x.color, op: x.op, visible: state.visible[x.id] !== false, geojson: state.geo[x.id] })));
        renderLayers(); renderLegend();
      };
      const tbl = el('button', 'mini', '▤');
      tbl.title = '属性表';
      tbl.onclick = e => { e.stopPropagation(); openTable(l.id); };
      const exp = el('button', 'mini', '↓');
      exp.title = '导出';
      exp.onclick = e => { e.stopPropagation(); exportMenu(e, l.id); };
      acts.append(eye, tbl, exp);
      item.append(sw, main, acts);
      item.onclick = () => {
        state.current = l.id;
        renderLayers();
        map.fitLayer(l.id);
        setStatus(`已选中「${l.name}」`);
      };
      box.appendChild(item);
    }
  }

  function renderLegend() {
    const lg = $('#legend');
    lg.innerHTML = '';
    const vis = state.layers.filter(l => state.geo[l.id]);
    if (!vis.length) { lg.style.display = 'none'; return; }
    lg.style.display = 'block';
    for (const l of vis) {
      const row = el('div', 'lg-row' + (state.visible[l.id] === false ? ' off' : ''));
      const sw = el('span', 'sw');
      sw.style.background = l.color;
      row.append(sw, el('span', '', l.name));
      row.onclick = () => {
        state.visible[l.id] = state.visible[l.id] === false;
        map.setLayers(state.layers.map(x => ({ id: x.id, name: x.name, color: x.color, op: x.op, visible: state.visible[x.id] !== false, geojson: state.geo[x.id] })));
        renderLegend(); renderLayers();
      };
      lg.appendChild(row);
    }
  }

  function setStatus(t) { $('#stMsg').textContent = t; }

  /* ------------------------------ 上传 / 示例 ------------------------------ */
  async function uploadFiles(files) {
    for (const f of files) {
      const fd = new FormData();
      fd.append('file', f);
      setStatus(`正在读取 ${f.name} …`);
      try {
        const r = await api('/api/upload', { method: 'POST', body: fd });
        if (r.need_choice) {
          setStatus('等待选择图层');
          openImport(r);
          return;
        }
        toast(r.message);
        await refresh(true);
        map.fitLayer(r.layer.id);
        state.current = r.layer.id;
        renderLayers();
      } catch (e) {
        toast('上传失败：' + e.message, true);
      }
    }
    setStatus('就绪');
  }

  /* 压缩包里有多个图层：弹窗让用户勾选 */
  let importJob = null;
  function openImport(res) {
    importJob = res.job;
    const list = $('#importList');
    list.innerHTML = '';
    $('#importHint').textContent = `压缩包里有 ${res.candidates.length} 个矢量图层，勾选需要的（可多选）。`;
    $('#importMeta').textContent = res.source || '';
    res.candidates.forEach((c, i) => {
      const row = el('label', 'pick-row' + (i === 0 ? ' on' : ''));
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = i === 0;            // 默认只勾选最大的那个，避免一下子载入几十万要素
      cb.value = String(c.id);
      cb.onchange = () => row.classList.toggle('on', cb.checked);
      const main = el('div', 'pick-main');
      main.appendChild(el('div', 'pick-name', c.name + c.ext));
      main.appendChild(el('div', 'pick-meta',
        `${(c.features || 0).toLocaleString('zh-CN')} 要素 · ${c.geom || '未知类型'}${c.crs ? ' · ' + c.crs : ''}`));
      row.append(cb, main, el('span', 'pick-size', `${c.size_mb} MB`));
      list.appendChild(row);
    });
    $('#importModal').hidden = false;
  }

  $('#importCancel').onclick = async () => {
    $('#importModal').hidden = true;
    if (importJob) await api('/api/import', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ job: importJob, picks: [] })
    }).catch(() => { });
    importJob = null;
    setStatus('就绪');
  };

  $('#importOk').onclick = async () => {
    if (!importJob) return;
    const picks = [...$('#importList').querySelectorAll('input:checked')].map(i => Number(i.value));
    if (!picks.length) { toast('至少选一个图层', true); return; }
    const btn = $('#importOk');
    btn.disabled = true;
    btn.textContent = '载入中…';
    $('#importModal').hidden = true;
    setStatus('正在载入所选图层…');
    try {
      const r = await api('/api/import', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ job: importJob, picks })
      });
      toast(r.message);
      await refresh(false);
      if (r.layers && r.layers.length) {
        state.current = r.layers[r.layers.length - 1].id;
        renderLayers();
        map.fitLayer(state.current);
      }
    } catch (e) {
      toast('载入失败：' + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = '载入所选';
      importJob = null;
      setStatus('就绪');
    }
  };

  async function refresh(fit) {
    const r = await api('/api/layers');
    await syncLayers(r.layers, { fit });
  }

  $('#dropzone').onclick = () => $('#fileInput').click();
  $('#btnUpload').onclick = () => $('#fileInput').click();
  $('#btnSamples').onclick = async () => {
    try {
      setStatus('正在生成示例数据…');
      const r = await api('/api/samples', { method: 'POST' });
      toast(r.message);
      await refresh(true);
    } catch (e) { toast('载入失败：' + e.message, true); }
    setStatus('就绪');
  };

  $('#fileInput').onchange = e => { uploadFiles([...e.target.files]); e.target.value = ''; };

  const dz = $('#dropzone');
  ['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => { if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]); });
  window.addEventListener('dragover', e => e.preventDefault());
  window.addEventListener('drop', e => { e.preventDefault(); if (e.dataTransfer.files.length) uploadFiles([...e.dataTransfer.files]); });

  /* ------------------------------ 属性表 / 导出 ------------------------------ */
  async function openTable(id) {
    const l = state.layers.find(x => x.id === id);
    if (!l) return;
    $('#tableTitle').textContent = `属性表 · ${l.name}`;
    $('#tableBody').innerHTML = '<div style="padding:20px;color:#6b7c93">加载中…</div>';
    $('#tableModal').hidden = false;
    try {
      const r = await api(`/api/layers/${id}/table`);
      const thead = '<thead><tr>' + r.columns.map(c => `<th>${esc(c)}</th>`).join('') + '</tr></thead>';
      const tbody = '<tbody>' + r.rows.map(row =>
        '<tr>' + row.map(c => `<td>${esc(c === null ? '' : String(c))}</td>`).join('') + '</tr>').join('') + '</tbody>';
      $('#tableBody').innerHTML = `<table>${thead}${tbody}</table>`;
      $('#tableMeta').textContent = `共 ${r.total.toLocaleString('zh-CN')} 条 · 显示前 ${r.rows.length} 条 · ${r.crs}`;
    } catch (e) {
      $('#tableBody').innerHTML = `<div style="padding:20px;color:#d92d20">${esc(e.message)}</div>`;
    }
  }

  function exportMenu(ev, id) {
    document.querySelectorAll('.export-pop').forEach(n => n.remove());
    const pop = el('div', 'export-pop');
    Object.assign(pop.style, {
      position: 'fixed', background: '#fff', border: '1px solid #e3e8f0', borderRadius: '9px',
      boxShadow: '0 12px 30px -10px rgba(16,32,56,.35)', padding: '5px', zIndex: 300, minWidth: '132px'
    });
    [['geojson', 'GeoJSON (.geojson)'], ['gpkg', 'GeoPackage (.gpkg)'], ['shp', 'Shapefile (.zip)'], ['csv', 'CSV 属性表']].forEach(([fmt, label]) => {
      const b = el('button', 'tinybtn', label);
      Object.assign(b.style, { display: 'block', width: '100%', textAlign: 'left', border: 'none', background: 'transparent', margin: '1px 0' });
      b.onmouseenter = () => b.style.background = '#f2f6fd';
      b.onmouseleave = () => b.style.background = 'transparent';
      b.onclick = () => {
        window.open(`/api/layers/${id}/export?format=${fmt}`, '_blank');
        pop.remove();
      };
      pop.appendChild(b);
    });
    pop.style.left = Math.min(ev.clientX, window.innerWidth - 150) + 'px';
    pop.style.top = ev.clientY + 'px';
    document.body.appendChild(pop);
    setTimeout(() => document.addEventListener('click', function h() {
      pop.remove(); document.removeEventListener('click', h);
    }), 0);
  }

  /* ------------------------------ 算子库 ------------------------------ */
  async function renderTools() {
    const r = await api('/api/suggestions');
    $('#toolGrid').innerHTML = '';
    for (const t of r.tools) {
      const b = el('button', 'tool-chip', t.label);
      b.title = t.name;
      b.onclick = () => {
        const L = currentLayerName();
        const other = otherLayerName(L);
        let tpl = TEMPLATES[t.name] || (t.label + '「{L}」');
        tpl = tpl.replace(/\{L\}/g, L).replace(/\{O\}/g, other);
        $('#input').value = tpl;
        $('#input').focus();
        autoGrow($('#input'));
      };
      $('#toolGrid').appendChild(b);
    }
    $('#toolCount').textContent = r.tools.length;
    const chips = $('#chips');
    chips.innerHTML = '';
    r.suggestions.slice(0, 6).forEach(s => {
      const c = el('button', 'chip', s);
      c.onclick = () => { $('#input').value = s; $('#input').focus(); autoGrow($('#input')); };
      chips.appendChild(c);
    });
  }

  function currentLayerName() {
    const l = state.layers.find(x => x.id === state.current);
    return l ? l.name : (state.layers[0] ? state.layers[0].name : '图层');
  }

  function otherLayerName(exclude) {
    const l = state.layers.find(x => x.name !== exclude);
    return l ? l.name : '边界';
  }

  /* ------------------------------ 对话 ------------------------------ */
  function addMsg(role, text) {
    const box = $('#messages');
    const m = el('div', 'msg ' + role);
    const b = el('div', 'bubble');
    text.split('\n').forEach((line, i) => {
      const s = el('span', 'ln', line);
      if (i === 0) s.style.display = 'block';
      b.appendChild(s);
    });
    m.appendChild(b);
    box.appendChild(m);
    box.scrollTop = box.scrollHeight;
    return m;
  }

  function addTyping() {
    const box = $('#messages');
    const m = el('div', 'msg agent');
    const b = el('div', 'bubble');
    b.innerHTML = '<div class="typing"><i></i><i></i><i></i></div>';
    m.appendChild(b);
    box.appendChild(m);
    box.scrollTop = box.scrollHeight;
    return m;
  }

  function renderSteps(steps) {
    const wrap = el('div', 'steps');
    steps.forEach((s, i) => {
      const card = el('div', 'step');
      const head = el('div', 'step-head');
      head.append(
        el('span', 'step-idx', String(i + 1)),
        el('span', 'step-name', s.label || s.tool)
      );
      const st = el('span', 'step-status' + (s.status === 'error' ? ' error' : ''),
        s.status === 'error' ? '失败' : `完成 ${(s.elapsed || 0).toFixed(2)}s`);
      head.appendChild(st);
      const body = el('div', 'step-body');
      body.appendChild(el('div', '', s.message || ''));

      if (s.data && typeof s.data === 'object') {
        const kv = el('div', 'kv');
        const d = s.data;
        if (d.features_in !== undefined && d.features_out !== undefined) {
          kv.appendChild(el('span', '', `要素 ${fmtNum(d.features_in)} → ${fmtNum(d.features_out)}`));
        }
        Object.entries(d).forEach(([k, v]) => {
          if (['features_in', 'features_out', 'table', 'meta', 'unit', 'total'].includes(k)) return;
          if (v === null || v === undefined || typeof v === 'object') return;
          const label = LABELS[k] || k;
          const val = /_m$|_m2$/.test(k) ? fmtMeters(v) : fmtNum(v);
          kv.appendChild(el('span', '', `${label} ${val}`));
        });
        if (kv.children.length) body.appendChild(kv);
      }

      if (s.warnings && s.warnings.length) {
        s.warnings.forEach(w => body.appendChild(el('div', 'step-warn', '⚠ ' + w)));
      }

      if (s.data && s.data.table && s.data.table.columns) {
        body.appendChild(miniTable(s.data.table));
      }

      if (s.layer) {
        const tag = el('div', 'newlayer-tag');
        tag.textContent = `◈ 新图层「${s.layer.name}」· ${s.layer.count} 要素`;
        body.appendChild(tag);
      }

      const acts = el('div', 'step-act');
      if (s.layer) {
        const b1 = el('button', 'tinybtn', '定位图层');
        b1.onclick = () => { state.current = s.layer.id; map.fitLayer(s.layer.id); renderLayers(); };
        const b2 = el('button', 'tinybtn', '属性表');
        b2.onclick = () => openTable(s.layer.id);
        const b3 = el('button', 'tinybtn', '导出 GeoJSON');
        b3.onclick = () => window.open(`/api/layers/${s.layer.id}/export?format=geojson`, '_blank');
        acts.append(b1, b2, b3);
      }
      if (s.tool === 'export' && s.data && s.data.layer_id) {
        const b = el('button', 'tinybtn', `下载 ${s.data.format.toUpperCase()}`);
        b.onclick = () => window.open(`/api/layers/${s.data.layer_id}/export?format=${s.data.format}`, '_blank');
        acts.appendChild(b);
      }
      if (acts.children.length) body.appendChild(acts);

      head.onclick = () => card.classList.toggle('collapsed');
      card.append(head, body);
      wrap.appendChild(card);
    });
    return wrap;
  }

  function miniTable(t) {
    const wrap = el('div', 'mini-table');
    const table = document.createElement('table');
    const thead = document.createElement('thead');
    const tr = document.createElement('tr');
    t.columns.forEach(c => tr.appendChild(el('th', '', String(c))));
    thead.appendChild(tr);
    table.appendChild(thead);
    const tb = document.createElement('tbody');
    t.rows.slice(0, 8).forEach(row => {
      const r = document.createElement('tr');
      row.forEach(cell => {
        const v = typeof cell === 'number' ? fmtNum(cell) : cell;
        r.appendChild(el('td', '', v === null ? '' : String(v)));
      });
      tb.appendChild(r);
    });
    table.appendChild(tb);
    wrap.appendChild(table);
    return wrap;
  }

  async function send() {
    const input = $('#input');
    const text = input.value.trim();
    if (!text || state.busy) return;
    state.busy = true;
    $('#send').disabled = true;
    input.value = '';
    autoGrow(input);
    addMsg('user', text);
    const typing = addTyping();
    setStatus('Agent 正在规划…');
    try {
      const r = await api('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, current: state.current })
      });
      typing.remove();
      const m = addMsg('agent', r.reply);
      if (r.steps && r.steps.length) {
        m.appendChild(renderSteps(r.steps));
      }
      const box = $('#messages');
      box.scrollTop = box.scrollHeight;
      if (r.layers) {
        const prevIds = new Set(state.layers.map(l => l.id));
        await syncLayers(r.layers);
        const created = r.steps.filter(s => s.layer).map(s => s.layer);
        if (created.length) {
          const last = created[created.length - 1];
          state.current = last.id;
          renderLayers();
          map.fitLayer(last.id);
        }
      }
      updatePill(r.planner);
      setStatus(r.steps && r.steps.length ? `完成 ${r.steps.length} 步` : '就绪');
    } catch (e) {
      typing.remove();
      addMsg('agent', '执行出错：' + e.message);
      setStatus('出错');
    } finally {
      state.busy = false;
      $('#send').disabled = false;
      input.focus();
    }
  }

  function updatePill(planner) {
    const p = $('#enginePill');
    const s = $('#plannerState');
    if (planner === 'llm') {
      p.className = 'pill on';
      p.innerHTML = '<i class="dot"></i>大模型规划';
      s.textContent = '大模型规划 · 已接入';
    } else {
      p.className = 'pill';
      p.innerHTML = '<i class="dot"></i>规则引擎';
      s.textContent = '规则引擎 · 离线可用';
    }
  }

  function autoGrow(t) {
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 96) + 'px';
  }

  $('#input').addEventListener('input', e => autoGrow(e.target));
  $('#input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $('#send').onclick = send;

  $('#btnClear').onclick = async () => {
    await api('/api/reset', { method: 'POST' });
    state.geo = {}; state.layers = []; state.visible = {}; state.current = null;
    $('#messages').innerHTML = '';
    await syncLayers([]);
    greet();
    toast('已清空图层与对话');
  };

  /* ------------------------------ 设置 ------------------------------ */
  $('#btnSettings').onclick = async () => {
    const c = await api('/api/settings');
    $('#cfgBase').value = c.base_url || '';
    $('#cfgModel').value = c.model || '';
    $('#cfgKey').value = '';
    $('#cfgKey').placeholder = c.has_key ? '已保存（留空则不修改）' : 'sk-...';
    $('#cfgEnabled').checked = !!c.enabled;
    $('#settingsModal').hidden = false;
  };

  document.querySelectorAll('[data-close]').forEach(b => {
    b.onclick = () => { b.closest('.modal-mask').hidden = true; };
  });
  document.querySelectorAll('.modal-mask').forEach(m => {
    m.addEventListener('mousedown', e => { if (e.target === m) m.hidden = true; });
  });

  const PRESETS = {
    deepseek: ['https://api.deepseek.com/v1', 'deepseek-chat'],
    dashscope: ['https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen-plus'],
    openai: ['https://api.openai.com/v1', 'gpt-4o-mini'],
    ollama: ['http://localhost:11434/v1', 'qwen2.5:7b']
  };
  document.querySelectorAll('.presets button').forEach(b => {
    b.onclick = () => {
      const [u, m] = PRESETS[b.dataset.preset];
      $('#cfgBase').value = u; $('#cfgModel').value = m;
      if (b.dataset.preset === 'ollama') { $('#cfgKey').value = 'ollama'; }
      $('#cfgEnabled').checked = true;
    };
  });

  $('#saveSettings').onclick = async () => {
    const body = {
      base_url: $('#cfgBase').value.trim(),
      model: $('#cfgModel').value.trim(),
      api_key: $('#cfgKey').value.trim() || '__KEEP__',
      enabled: $('#cfgEnabled').checked
    };
    await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    $('#settingsModal').hidden = true;
    const c = await api('/api/settings');
    updatePill(c.ready ? 'llm' : 'rule');
    toast(c.ready ? '已启用大模型规划'
      : (body.enabled ? '已保存：还缺接口地址或 API Key，暂时仍用规则引擎' : '已保存设置'));
  };

  /* ------------------------------ 快捷按钮 ------------------------------ */
  $('#zoomIn').onclick = () => map.zoomBy(1);
  $('#zoomOut').onclick = () => map.zoomBy(-1);
  $('#zoomFit').onclick = () => map.fitAll();
  window.addEventListener('resize', () => map.resize());

  /* ------------------------------ 启动 ------------------------------ */
  function greet() {
    addMsg('agent', '我是你的空间分析助手。数据拖进来后，直接用中文说需求就行，例如：\n· 对「地块」做 500 米缓冲区，按用地类型融合\n· 用「行政区」裁剪「地块」，再统计各类面积\n· 给「学校」做 1 公里覆盖圈，看覆盖了哪些地块\n\n每一步运算都会生成新图层，原数据只读不改。');
    $('#messages').lastChild && $('#messages').lastChild.classList.add('welcome');
  }

  async function boot() {
    const c = await api('/api/settings');
    updatePill(c.ready ? 'llm' : 'rule');
    await renderTools();
    const r = await api('/api/layers');
    await syncLayers(r.layers, { fit: true });
    greet();
    if (!r.layers.length) setStatus('先上传数据或载入示例数据');

    // 演示/测试入口：?q=指令&auto=1 自动执行一次对话
    const qs = new URLSearchParams(location.search);
    if (qs.get('q')) {
      $('#input').value = qs.get('q');
      autoGrow($('#input'));
      if (qs.get('auto') === '1') setTimeout(send, 300);
    }
    if (qs.get('open') === 'settings') $('#btnSettings').click();
    if (qs.get('open') === 'table' && state.current) setTimeout(() => openTable(state.current), 400);
  }

  boot().catch(e => { console.error(e); toast('初始化失败：' + e.message, true); });
})();
