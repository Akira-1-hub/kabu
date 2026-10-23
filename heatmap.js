/*
 市場ヒートマップ（依存ライブラリなし）
   makeHeatmap(container, rows, opts)
     rows : [{code,name,sector,cap(億円),pct(前日比%),sr(空売り比率%),close,vr}]
     opts : { link:(code)=>url }
 業種ごとにまとめ、タイル面積＝時価総額、色＝前日比(発散)/空売り比率(順次)。
 配色: 発散＝暖色↔中立グレー↔寒色（--rise/--fall から生成するので色反転にも追従）
       順次＝単一色相の明度段階。分類は7以下。凡例と表ビューを常設。
*/
function makeHeatmap(container, rows, opts) {
  opts = opts || {};
  const link = opts.link || (c => '/stock/' + c);
  _hmStyle();

  // ---- 配色 -------------------------------------------------------------
  const NEUTRAL = '#3a3a52';                       // 発散の中立（無彩色）
  const cssVar = (n, fb) => {
    try { return getComputedStyle(document.documentElement).getPropertyValue(n).trim() || fb; }
    catch (e) { return fb; }
  };
  const hex2rgb = h => {
    h = (h || '').replace('#', '');
    if (h.length === 3) h = h.split('').map(c => c + c).join('');
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  };
  const mix = (a, b, t) => {
    const A = hex2rgb(a), B = hex2rgb(b);
    return '#' + [0, 1, 2].map(i =>
      Math.round(A[i] + (B[i] - A[i]) * t).toString(16).padStart(2, '0')).join('');
  };
  // 中立→極の3段階（明度が単調に上がる＝順序が色で読める）
  const arm = pole => [0.38, 0.68, 1].map(t => mix(NEUTRAL, pole, t));

  function ramps() {
    const up = arm(cssVar('--rise', '#ff6b6b'));    // 上昇（暖色）
    const dn = arm(cssVar('--fall', '#4fc3f7'));    // 下落（寒色）
    return {
      // 発散: 7分類（下3・中立・上3）
      pct: {
        bins: [-5, -2, -0.3, 0.3, 2, 5],
        colors: [dn[2], dn[1], dn[0], NEUTRAL, up[0], up[1], up[2]],
        labels: ['-5%以下', '-5〜-2%', '-2〜-0.3%', '±0.3%', '+0.3〜2%', '+2〜5%', '+5%以上'],
        fmt: v => v == null ? '-' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%',
      },
      // 順次: 単一色相5段階（薄→濃）
      sr: {
        bins: [0.5, 1, 2, 5],
        colors: [0.18, 0.4, 0.62, 0.82, 1].map(t => mix(NEUTRAL, '#b39dff', t)),
        labels: ['0.5%未満', '0.5〜1%', '1〜2%', '2〜5%', '5%以上'],
        fmt: v => v == null ? '-' : v.toFixed(2) + '%',
      },
    };
  }
  const binOf = (v, bins) => { let i = 0; while (i < bins.length && v >= bins[i]) i++; return i; };

  // ---- 状態 -------------------------------------------------------------
  let mode = 'pct';       // pct | sr
  let view = 'map';       // map | table
  let topN = 300;
  let sectorFilter = '';

  const base = (rows || []).filter(r => r.cap && r.cap > 0);
  const sectors = [...new Set(base.map(r => r.sector))].sort();

  function current() {
    let d = base.filter(r => (mode === 'pct' ? r.pct != null : r.sr != null));
    if (sectorFilter) d = d.filter(r => r.sector === sectorFilter);
    return d.sort((a, b) => b.cap - a.cap).slice(0, topN);
  }

  // ---- スクエアリファイド・ツリーマップ ---------------------------------
  function worstRatio(row, side) {
    const s = row.reduce((a, d) => a + d.area, 0);
    const mx = Math.max(...row.map(d => d.area));
    const mn = Math.min(...row.map(d => d.area));
    return Math.max((side * side * mx) / (s * s), (s * s) / (side * side * mn));
  }
  function layoutRow(row, x, y, w, h) {
    const s = row.reduce((a, d) => a + d.area, 0);
    const placed = [];
    if (w >= h) {
      const rw = s / h; let cy = y;
      for (const d of row) { const rh = d.area / rw; placed.push({ d, rect: { x, y: cy, w: rw, h: rh } }); cy += rh; }
      return { placed, rest: { x: x + rw, y, w: w - rw, h } };
    }
    const rh = s / w; let cx = x;
    for (const d of row) { const rw = d.area / rh; placed.push({ d, rect: { x: cx, y, w: rw, h: rh } }); cx += rw; }
    return { placed, rest: { x, y: y + rh, w, h: h - rh } };
  }
  function squarify(items, rect) {
    const out = [];
    const total = items.reduce((s, d) => s + d.value, 0);
    if (!total || rect.w <= 0 || rect.h <= 0) return out;
    const scale = (rect.w * rect.h) / total;
    let q = items.map(d => ({ ...d, area: d.value * scale }));
    let { x, y, w, h } = rect, row = [];
    while (q.length) {
      const side = Math.min(w, h);
      if (!row.length || worstRatio(row.concat([q[0]]), side) <= worstRatio(row, side)) {
        row.push(q.shift());
      } else {
        const r = layoutRow(row, x, y, w, h);
        out.push(...r.placed); ({ x, y, w, h } = r.rest); row = [];
      }
    }
    if (row.length) out.push(...layoutRow(row, x, y, w, h).placed);
    return out;
  }

  // ---- 描画 -------------------------------------------------------------
  const GAP = 2, HEAD = 15;

  // タイル幅に収まるところまで銘柄名を詰める（はみ出し・文字切れを起こさない）
  const CW = 10, CW_ASCII = 5.6;          // 10pxフォントでの全角/半角のおよその幅
  const chw = ch => ch.charCodeAt(0) > 0x2E7F ? CW : CW_ASCII;
  function fit(s, avail) {
    s = String(s || '');
    let w = 0;
    for (let i = 0; i < s.length; i++) w += chw(s[i]);
    if (w <= avail) return esc(s);
    let out = '', acc = 0;
    const room = avail - CW_ASCII * 1.4;   // 省略記号のぶんを空けておく
    for (const ch of s) {
      const cw = chw(ch);
      if (acc + cw > room) break;
      out += ch; acc += cw;
    }
    // 1〜2文字だけ残っても意味が読めないので、その場合は出さない（詳細はツールチップ）
    return out.length >= 3 ? esc(out) + '…' : '';
  }
  const esc = s => String(s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

  function renderMap(host) {
    const data = current();
    const W = host.clientWidth || 900;
    const H = Math.max(420, Math.min(760, Math.round(W * 0.62)));
    host.style.height = H + 'px';
    if (!data.length) { host.innerHTML = '<div class="empty">該当する銘柄がありません</div>'; return; }

    const R = ramps()[mode];
    const byS = {};
    data.forEach(r => { (byS[r.sector] = byS[r.sector] || []).push(r); });
    const groups = Object.entries(byS)
      .map(([s, list]) => ({ key: s, value: list.reduce((a, r) => a + r.cap, 0), list }))
      .sort((a, b) => b.value - a.value);

    let html = '';
    for (const g of squarify(groups, { x: 0, y: 0, w: W, h: H })) {
      const { x, y, w, h } = g.rect;
      if (w < 6 || h < 6) continue;
      const showHead = h > HEAD + 12 && w > 46;
      const inner = { x: 0, y: showHead ? HEAD : 0, w: w - GAP, h: h - (showHead ? HEAD : 0) - GAP };
      html += `<div class="hm-sec" style="left:${x}px;top:${y}px;width:${w - GAP}px;height:${h - GAP}px">`;
      if (showHead) html += `<div class="hm-sec-t">${esc(g.d.key)}</div>`;
      const tiles = squarify(g.d.list.map(r => ({ value: r.cap, r })), inner);
      for (const t of tiles) {
        const r = t.d.r, q = t.rect;
        if (q.w < 3 || q.h < 3) continue;
        const v = mode === 'pct' ? r.pct : r.sr;
        const col = R.colors[binOf(v, R.bins)];
        const tw = q.w - GAP, th = q.h - GAP;
        // 銘柄名が入る時だけラベルを出す（名前なしで数値だけ残さない。詳細はツールチップ）
        let label = '';
        if (tw >= 28 && th >= 15) {
          const nm = fit(r.name || r.code, tw - 6);
          if (nm) {
            label = `<span class="hm-c">${nm}</span>` +
                    (tw >= 40 && th >= 28 ? `<span class="hm-v">${R.fmt(v)}</span>` : '');
          }
        }
        html += `<a class="hm-t" href="${link(r.code)}" data-code="${r.code}"
          style="left:${q.x}px;top:${q.y}px;width:${tw}px;height:${th}px;background:${col}">${label}</a>`;
      }
      html += '</div>';
    }
    host.innerHTML = html;

    // ツールチップ（カーソル右上・はみ出す側は反転）
    const tip = document.createElement('div');
    tip.className = 'hm-tip';
    host.appendChild(tip);
    const by = {}; data.forEach(r => by[r.code] = r);
    host.querySelectorAll('.hm-t').forEach(el => {
      el.addEventListener('mousemove', e => {
        const r = by[el.dataset.code]; if (!r) return;
        tip.innerHTML =
          `<b>${r.code} ${esc(r.name)}</b><br>` +
          `<span class="muted">${esc(r.sector)}</span><br>` +
          `前日比 <b>${ramps().pct.fmt(r.pct)}</b>　終値 ${r.close == null ? '-' : Math.round(r.close).toLocaleString()}<br>` +
          `空売り <b>${r.sr == null ? '-' : r.sr.toFixed(2) + '%'}</b>　時価総額 <b>${fmtCapJ(r.cap)}</b>`;
        tip.style.display = 'block';
        const hb = host.getBoundingClientRect();
        let lx = e.clientX - hb.left + 14, ty = e.clientY - hb.top - tip.offsetHeight - 12;
        if (lx + tip.offsetWidth > hb.width - 4) lx = e.clientX - hb.left - tip.offsetWidth - 14;
        if (lx < 2) lx = 2;
        if (ty < 2) ty = e.clientY - hb.top + 16;
        tip.style.left = lx + 'px'; tip.style.top = ty + 'px';
      });
      el.addEventListener('mouseleave', () => { tip.style.display = 'none'; });
    });
  }

  function fmtCapJ(v) {
    if (v == null) return '-';
    return v >= 10000 ? (v / 10000).toFixed(2) + '兆円' : Math.round(v).toLocaleString() + '億円';
  }

  function renderTable(host) {
    const data = current();
    host.style.height = 'auto';
    const R = ramps()[mode];
    host.innerHTML =
      '<div class="hm-table"><table><thead><tr>' +
      '<th class="txt-left">コード</th><th class="txt-left">銘柄名</th><th class="txt-left">業種</th>' +
      '<th>前日比%</th><th>空売り%</th><th>時価総額</th></tr></thead><tbody>' +
      data.map(r => `<tr>
        <td class="txt-left code"><a href="${link(r.code)}">${r.code}</a></td>
        <td class="txt-left">${esc(r.name)}</td>
        <td class="txt-left muted">${esc(r.sector)}</td>
        <td class="${r.pct > 0 ? 'rise' : (r.pct < 0 ? 'fall' : 'muted')}">${ramps().pct.fmt(r.pct)}</td>
        <td class="${r.sr >= 2 ? 'rise' : 'muted'}">${r.sr == null ? '-' : r.sr.toFixed(2) + '%'}</td>
        <td>${fmtCapJ(r.cap)}</td></tr>`).join('') +
      '</tbody></table></div>';
  }

  function legendHtml() {
    const R = ramps()[mode];
    return '<div class="hm-leg">' +
      `<span class="muted">${mode === 'pct' ? '前日比' : '空売り比率'}</span>` +
      R.colors.map((c, i) =>
        `<span class="hm-leg-i"><i style="background:${c}"></i>${R.labels[i]}</span>`).join('') +
      '</div>';
  }

  function render() {
    const data = current();
    container.innerHTML =
      `<div class="hm-ctl">
         <span class="badge accent">色</span>
         <select class="hm-mode">
           <option value="pct"${mode === 'pct' ? ' selected' : ''}>前日比</option>
           <option value="sr"${mode === 'sr' ? ' selected' : ''}>空売り比率</option>
         </select>
         <span class="muted" style="font-size:12px">業種</span>
         <select class="hm-sec-f">
           <option value="">すべて</option>
           ${sectors.map(s => `<option value="${s}"${s === sectorFilter ? ' selected' : ''}>${s}</option>`).join('')}
         </select>
         <span class="muted" style="font-size:12px">表示</span>
         <select class="hm-n">
           ${[100, 200, 300, 500, 1000].map(n =>
             `<option value="${n}"${n === topN ? ' selected' : ''}>時価総額 上位${n}</option>`).join('')}
         </select>
         <span class="spacer" style="flex:1"></span>
         <button class="hm-view${view === 'map' ? ' on' : ''}" data-v="map">ヒートマップ</button>
         <button class="hm-view${view === 'table' ? ' on' : ''}" data-v="table">表</button>
       </div>
       ${legendHtml()}
       <div class="hm-host"></div>
       <div class="muted hm-note">※面積＝時価総額、色＝${mode === 'pct' ? '前日比（暖色=上昇 / 寒色=下落）' : '空売り比率（濃いほど厚い）'}。${data.length}銘柄。タイルをクリックで銘柄詳細。</div>`;

    const host = container.querySelector('.hm-host');
    (view === 'map' ? renderMap : renderTable)(host);

    container.querySelector('.hm-mode').onchange = e => { mode = e.target.value; render(); };
    container.querySelector('.hm-sec-f').onchange = e => { sectorFilter = e.target.value; render(); };
    container.querySelector('.hm-n').onchange = e => { topN = +e.target.value; render(); };
    container.querySelectorAll('.hm-view').forEach(b => {
      b.onclick = () => { view = b.dataset.v; render(); };
    });
  }

  let rto;
  window.addEventListener('resize', () => {
    clearTimeout(rto);
    rto = setTimeout(() => { if (view === 'map') renderMap(container.querySelector('.hm-host')); }, 150);
  });

  render();
  return { refresh: render };
}

function _hmStyle() {
  if (document.getElementById('hm-style')) return;
  const el = document.createElement('style');
  el.id = 'hm-style';
  el.textContent = `
.hm-ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;background:var(--bg3);
  border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:8px}
.hm-ctl select{background:var(--bg);color:var(--text);border:1px solid var(--border);
  border-radius:5px;padding:5px 8px;font-size:12px}
.hm-ctl .hm-view{background:var(--bg);color:var(--muted);border:1px solid var(--border);
  border-radius:6px;padding:5px 12px;font-size:12px;cursor:pointer}
.hm-ctl .hm-view.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}
.hm-leg{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:11px;
  color:var(--muted);margin-bottom:8px;padding:0 2px}
.hm-leg-i{display:flex;align-items:center;gap:4px}
.hm-leg-i i{width:16px;height:10px;border-radius:2px;display:inline-block}
.hm-host{position:relative;width:100%}
.hm-sec{position:absolute;overflow:hidden;background:var(--bg2);border-radius:3px}
.hm-sec-t{position:absolute;left:0;top:0;right:0;height:15px;line-height:15px;
  font-size:10px;color:var(--muted);padding:0 4px;white-space:nowrap;overflow:hidden}
.hm-t{position:absolute;border-radius:2px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;overflow:hidden;text-decoration:none;
  line-height:1.15;transition:filter .1s}
.hm-t:hover{filter:brightness(1.35);text-decoration:none}
.hm-t .hm-c,.hm-t .hm-v{max-width:100%;padding:0 2px;white-space:nowrap;overflow:hidden;
  color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.55);font-size:10px}
.hm-t .hm-c{font-weight:700}
.hm-tip{position:absolute;background:rgba(20,20,40,.95);border:1px solid var(--border);
  border-radius:6px;padding:6px 9px;font-size:11px;color:var(--text);pointer-events:none;
  white-space:nowrap;z-index:20;display:none;line-height:1.6}
.hm-table{max-height:640px;overflow:auto}
.hm-note{font-size:11px;margin-top:6px}
@media(max-width:700px){.hm-leg{font-size:10px;gap:6px}}
`;
  document.head.appendChild(el);
}
