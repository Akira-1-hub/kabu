/*
 日経平均の寄与度（依存ライブラリなし）。ツール(/nikkei)と公開サイト(#nikkei)で共通
   makeNikkei(container, data, opts)
     data : nikkei.compute() の結果（ツールは /api/nikkei、公開サイトは nikkei.json）
     opts : { link:(code)=>url }
 期間を選ぶと、その間の毎日の寄与度を足し合わせる（騰落率は日々の前日比を掛け合わせる）。
 色は --rise / --fall を使うので、色反転にも追従する。
*/
function makeNikkei(container, data, opts) {
  opts = opts || {};
  const link = opts.link || (c => '/stock/' + c);
  _nkStyle();
  const D = data || {};
  const days = D.days || [];
  if (!days.length) {
    container.innerHTML = '<div class="empty">まだ計算できる日がありません（日経のウエートと株価がそろうと表示されます）</div>';
    return null;
  }

  // ---- 状態 ----
  let at = 0;            // 選んだ日（days の添字。0 が最新）
  let span = 1;          // 期間（営業日数）
  let sortK = 'c', sortD = true, kw = '';
  let A = null;          // いま表示している集計

  // ---- 書式 ----
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
    ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
  const cls = v => v == null ? 'muted' : v > 0 ? 'rise' : v < 0 ? 'fall' : 'muted';
  const num = (v, d) => Number(v).toLocaleString('ja-JP',
    { minimumFractionDigits: d, maximumFractionDigits: d });
  const sgn = (v, d) => v == null ? '-' : (v > 0 ? '+' : '') + num(v, d);
  const yen = v => v == null ? '-' : sgn(v, 2) + '円';
  const pct = v => v == null ? '-' : sgn(v, 2) + '%';
  const WD = '日月火水木金土';
  const wd = s => { const [y, m, d] = s.split('-').map(Number); return WD[new Date(y, m - 1, d).getDay()]; };
  const md = s => s.slice(5).replace('-', '/');

  // ---- 集計（選んだ日から span 日さかのぼって足す）----
  function agg() {
    const sel = days.slice(at, at + span);          // 新しい順
    const by = new Map();
    let est = 0, chg = 0, chgOk = true;
    const warn = new Set(), miss = new Set();
    sel.forEach(day => {
      est += day.est;
      if (day.chg == null) chgOk = false; else chg += day.chg;
      (day.warn || []).forEach(w => warn.add(w));
      (day.miss || []).forEach(c => miss.add(c));
      day.i.forEach((ci, k) => {
        let o = by.get(ci);
        if (!o) { o = { ci, c: 0, g: 1, n: 0, px: null, w: null }; by.set(ci, o); }
        o.c += day.c[k];
        if (day.p[k] != null) { o.g *= 1 + day.p[k] / 100; o.n++; }
      });
    });
    const end = sel[0];
    end.i.forEach((ci, k) => { const o = by.get(ci); o.px = end.px[k]; o.w = end.w[k]; });
    const rows = [...by.values()].map(o => ({
      code: D.codes[o.ci], name: D.names[o.ci], sec: D.secs[o.ci],
      c: Math.round(o.c * 100) / 100, p: o.n ? (o.g - 1) * 100 : null, px: o.px, w: o.w,
    }));
    [...rows].sort((a, b) => b.c - a.c).forEach((r, i) => { r.rank = i + 1; });
    return {
      sel, rows, est, warn, miss, end,
      chg: chgOk ? chg : null,
      nkStart: chgOk && end.nk != null ? end.nk - chg : null,
      from: sel[sel.length - 1].d,
    };
  }

  // ---- 部品 ----
  function tile(lbl, main, mainCls, sub) {
    return `<div class="nk-tile"><div class="lbl">${lbl}</div>
      <div class="num ${mainCls || ''}">${main}</div><div class="sub">${sub || ''}</div></div>`;
  }

  function barRow(r, max) {
    const w = max ? Math.abs(r.c) / max * 100 : 0;
    const col = r.c >= 0 ? 'var(--rise)' : 'var(--fall)';
    return `<div class="nk-row">
      <a class="nm" href="${link(r.code)}" title="${esc(r.code + ' ' + r.name)}">${esc(r.name)}</a>
      <span class="nk-track"><span class="nk-fill" style="width:${w.toFixed(1)}%;background:${col}"></span></span>
      <span class="v ${cls(r.c)}">${yen(r.c)}</span>
      <span class="s ${cls(r.p)}">${pct(r.p)}</span></div>`;
  }

  function topCard(title, list, max, empty) {
    return `<div class="card"><h2>${title}</h2><div class="nk-list">
      ${list.length ? list.map(r => barRow(r, max)).join('') : `<div class="empty" style="padding:20px">${empty}</div>`}
      </div></div>`;
  }

  function sectorCard(rows) {
    const m = new Map();
    rows.forEach(r => {
      const o = m.get(r.sec) || { sec: r.sec, c: 0, n: 0, up: 0, dn: 0 };
      o.c += r.c; o.n++; if (r.c > 0) o.up++; else if (r.c < 0) o.dn++;
      m.set(r.sec, o);
    });
    const list = [...m.values()].sort((a, b) => b.c - a.c);
    const max = Math.max(...list.map(o => Math.abs(o.c)), 0.01);
    return `<div class="card" style="margin-bottom:14px"><h2>業種別の寄与度<span class="muted" style="font-weight:400">東証33業種・（ ）は銘柄数</span></h2>
      <div class="nk-list nk-sec">${list.map(o => {
        const w = (Math.abs(o.c) / max * 100).toFixed(1);
        const fill = `<span class="nk-fill" style="width:${w}%;background:${o.c >= 0 ? 'var(--rise)' : 'var(--fall)'}"></span>`;
        return `<div class="nk-row">
          <span class="nm" title="上げ${o.up}・下げ${o.dn}">${esc(o.sec)} <span class="muted">(${o.n})</span></span>
          <span class="nk-div"><span class="l">${o.c < 0 ? fill : ''}</span><span class="r">${o.c >= 0 ? fill : ''}</span></span>
          <span class="v ${cls(o.c)}">${yen(Math.round(o.c * 100) / 100)}</span>
          <span class="s muted">${o.up}↑${o.dn}↓</span></div>`;
      }).join('')}</div></div>`;
  }

  function noteLine() {
    const parts = [];
    let s = `寄与度の合計 <b class="${cls(A.est)}">${yen(Math.round(A.est * 100) / 100)}</b>`;
    if (A.chg != null) {
      const diff = Math.round((A.chg - A.est) * 100) / 100;
      s += ` ／ 実際の値動き <b class="${cls(A.chg)}">${yen(Math.round(A.chg * 100) / 100)}</b>（差 ${sgn(diff, 2)}円）`;
    }
    parts.push(s);
    if (A.sel.length < span) parts.push(`<span class="warn">データがあるのは${A.sel.length}日分です</span>`);
    if (A.warn.has('diff')) parts.push('<span class="warn">実際の値動きと合わない日があります。構成銘柄の入れ替えがあった可能性があり、次の月末のウエートが公表されると直ります</span>');
    if (A.warn.has('miss')) parts.push(`<span class="warn">株価が取れていない銘柄があります（${[...A.miss].slice(0, 8).map(esc).join('・')}${A.miss.size > 8 ? ' ほか' : ''}）。翌日の更新で埋まります</span>`);
    if (A.warn.has('nk')) parts.push('<span class="warn">日経平均の終値がまだ取れていない日があり、その日は実際の値動きと比べていません</span>');
    return parts.join('<br>');
  }

  // ---- 全体 ----
  function render() {
    A = agg();
    const rows = A.rows;
    const up = rows.filter(r => r.c > 0), dn = rows.filter(r => r.c < 0);
    const sum = l => l.reduce((s, r) => s + r.c, 0);
    const topUp = [...up].sort((a, b) => b.c - a.c).slice(0, 10);
    const topDn = [...dn].sort((a, b) => a.c - b.c).slice(0, 10);
    const max = Math.max(...topUp.concat(topDn).map(r => Math.abs(r.c)), 0.01);
    const heavy = rows.filter(r => r.w != null).sort((a, b) => b.w - a.w).slice(0, 5);
    const heavySum = sum(heavy);
    const chgPct = A.chg != null && A.nkStart ? A.chg / A.nkStart * 100 : null;
    const range = span === 1 ? `${md(A.end.d)}（${wd(A.end.d)}）` : `${md(A.from)}〜${md(A.end.d)}`;

    container.innerHTML = `
      <div class="nk-ctl">
        <button class="btn btn-sm btn-ghost" data-act="older" title="前の日" ${at + 1 >= days.length ? 'disabled' : ''}>◀</button>
        <select class="nk-date" aria-label="日付">${days.map((d, i) =>
          `<option value="${i}" ${i === at ? 'selected' : ''}>${d.d}（${wd(d.d)}）</option>`).join('')}</select>
        <button class="btn btn-sm btn-ghost" data-act="newer" title="次の日" ${at === 0 ? 'disabled' : ''}>▶</button>
        <span class="nk-seg">${[1, 5, 20].map(n =>
          `<button class="btn btn-sm btn-ghost ${n === span ? 'on' : ''}" data-span="${n}">${n === 1 ? '1日' : n + '日間'}</button>`).join('')}</span>
      </div>
      <div class="nk-tiles">
        ${tile(span === 1 ? '日経平均（前日比）' : `日経平均（${range}）`,
               A.end.nk != null ? num(A.end.nk, 2) : '-', '',
               A.chg != null ? `<span class="${cls(A.chg)}"><b>${sgn(A.chg, 2)}</b>（${pct(chgPct)}）</span>` : '終値がまだありません')}
        ${tile('押し上げた銘柄の合計', yen(Math.round(sum(up) * 100) / 100), 'rise', `${up.length}銘柄`)}
        ${tile('押し下げた銘柄の合計', yen(Math.round(sum(dn) * 100) / 100), 'fall', `${dn.length}銘柄`)}
        ${tile('ウエート上位5銘柄', yen(Math.round(heavySum * 100) / 100), cls(heavySum),
               `<span title="${esc(heavy.map(r => r.name).join('・'))}">残り${rows.length - heavy.length}銘柄で ${yen(Math.round((A.est - heavySum) * 100) / 100)}</span>`)}
      </div>
      <div class="nk-note">${noteLine()}</div>
      <div class="grid grid-2" style="margin-bottom:14px">
        ${topCard(`▲ 押し上げ TOP10<span class="muted" style="font-weight:400">${range}</span>`, topUp, max, '押し上げた銘柄はありません')}
        ${topCard(`▼ 押し下げ TOP10<span class="muted" style="font-weight:400">${range}</span>`, topDn, max, '押し下げた銘柄はありません')}
      </div>
      ${sectorCard(rows)}
      <div class="card">
        <h2><span>全${rows.length}銘柄</span>
          <input class="nk-kw" type="search" placeholder="コード・銘柄名・業種" value="${esc(kw)}" aria-label="絞り込み"></h2>
        <div class="table-wrap"><table class="nk-tbl">
          <thead><tr>
            <th class="hide-sp" data-k="rank">順位</th>
            <th class="txt-left" data-k="code">コード</th>
            <th class="txt-left" data-k="name">銘柄名</th>
            <th class="txt-left hide-sp" data-k="sec">業種</th>
            <th class="hide-sp" data-k="px">終値</th>
            <th data-k="p">${span === 1 ? '前日比' : '騰落率'}</th>
            <th data-k="c">寄与度(円)</th>
            <th data-k="w" title="${esc(A.end.d)} 終値時点の推定">ウエート</th>
          </tr></thead>
          <tbody class="nk-body"></tbody>
        </table></div>
      </div>
      <div class="muted" style="font-size:11px;margin-top:8px;line-height:1.7">
        ※寄与度＝前日の日経平均 × その銘柄のウエート × 前日比（円）。期間を選ぶと、その間の毎日の寄与度を足し合わせます。<br>
        ※ウエートは日本経済新聞社が公表する月末時点の値（基準日 ${esc((D.bases || [])[0] || '-')}）を、日々の値動きで伸ばした推定です。
        構成銘柄の入れ替えがあった日は、次の公表まで合わないことがあります。株価は株探、日経平均の終値は Yahoo Finance。
      </div>`;

    container.querySelector('.nk-date').onchange = e => { at = +e.target.value; render(); };
    container.querySelectorAll('[data-act]').forEach(b => b.onclick = () => {
      at = Math.min(days.length - 1, Math.max(0, at + (b.dataset.act === 'older' ? 1 : -1)));
      render();
    });
    container.querySelectorAll('[data-span]').forEach(b => b.onclick = () => { span = +b.dataset.span; render(); });
    container.querySelector('.nk-kw').oninput = e => { kw = e.target.value.trim(); renderTable(); };
    container.querySelectorAll('th[data-k]').forEach(th => th.onclick = () => {
      const k = th.dataset.k;
      if (sortK === k) sortD = !sortD;
      else { sortK = k; sortD = !['rank', 'code', 'name', 'sec'].includes(k); }
      renderTable();
    });
    renderTable();
  }

  function renderTable() {
    let rows = A.rows;
    if (kw) rows = rows.filter(r => (r.code + ' ' + r.name + ' ' + r.sec).includes(kw));
    const k = sortK, dir = sortD ? -1 : 1;
    rows = [...rows].sort((a, b) => {
      const x = a[k], y = b[k];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (typeof x === 'string' ? x.localeCompare(y, 'ja') : x - y) * dir;
    });
    container.querySelectorAll('th[data-k]').forEach(th => {
      th.textContent = th.textContent.replace(/[▲▼]$/, '') + (th.dataset.k === sortK ? (sortD ? '▼' : '▲') : '');
    });
    container.querySelector('.nk-body').innerHTML = rows.length ? rows.map(r => `<tr>
      <td class="hide-sp muted">${r.rank}</td>
      <td class="code txt-left"><a href="${link(r.code)}">${esc(r.code)}</a></td>
      <td class="txt-left nk-nm" title="${esc(r.name)}"><b>${esc(r.name)}</b></td>
      <td class="txt-left hide-sp muted">${esc(r.sec)}</td>
      <td class="hide-sp">${r.px == null ? '-' : num(r.px, r.px % 1 ? 1 : 0)}</td>
      <td class="${cls(r.p)}">${pct(r.p)}</td>
      <td class="${cls(r.c)}"><b>${sgn(r.c, 2)}</b></td>
      <td>${r.w == null ? '-' : num(r.w, 2) + '%'}</td></tr>`).join('')
      : '<tr><td colspan="8" class="empty">該当する銘柄はありません</td></tr>';
  }

  render();
  return { render };
}

function _nkStyle() {
  if (document.getElementById('nk-style')) return;
  const st = document.createElement('style');
  st.id = 'nk-style';
  st.textContent = `
.nk-ctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.nk-ctl select{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:5px;padding:5px 8px;font-size:13px}
.nk-ctl .btn[disabled]{opacity:.35;cursor:default}
.nk-seg{display:inline-flex;gap:4px;margin-left:4px}
.nk-seg .on{background:var(--accent);border-color:var(--accent);color:#fff}
.nk-tiles{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:8px}
.nk-tile{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:12px 14px;min-width:0}
.nk-tile .lbl{color:var(--muted);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nk-tile .num{font-size:22px;font-weight:700;margin-top:2px;white-space:nowrap}
.nk-tile .sub{font-size:12px;margin-top:2px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.nk-note{font-size:12px;color:var(--muted);margin:0 0 14px;line-height:1.7}
.nk-note .warn{color:var(--both)}
.nk-list{padding:6px 14px 10px}
.nk-row{display:grid;grid-template-columns:minmax(0,10em) minmax(0,1fr) 6.8em 4.4em;gap:8px;align-items:center;padding:4px 0;font-size:13px}
.nk-sec .nk-row{grid-template-columns:minmax(0,10em) minmax(0,1fr) 6.8em 4.4em}
.nk-row .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.nk-track{height:12px;background:var(--bg3);border-radius:3px;overflow:hidden}
.nk-fill{display:block;height:100%;border-radius:3px;opacity:.85}
.nk-row .v{text-align:right;font-weight:700;white-space:nowrap}
.nk-row .s{text-align:right;font-size:11px;white-space:nowrap}
.nk-div{display:grid;grid-template-columns:1fr 1fr;height:12px}
.nk-div .l{display:flex;justify-content:flex-end;background:var(--bg3);border-radius:3px 0 0 3px;overflow:hidden}
.nk-div .r{display:flex;background:var(--bg3);border-radius:0 3px 3px 0;border-left:1px solid var(--muted);overflow:hidden}
.nk-kw{background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:5px;padding:4px 8px;font-size:12px;width:170px;max-width:50%}
@media(max-width:700px){
  .nk-tiles{grid-template-columns:repeat(2,minmax(0,1fr))}
  .nk-tile{padding:10px 12px}
  .nk-tile .num{font-size:18px}
  .nk-list{padding:6px 10px 10px}
  .nk-row,.nk-sec .nk-row{grid-template-columns:minmax(0,7.5em) minmax(0,1fr) 6.2em 3.9em;gap:6px;font-size:12px}
  .nk-tbl thead th,.nk-tbl tbody td{padding:7px 5px}
  .nk-tbl td.nk-nm{max-width:6.5em;overflow:hidden;text-overflow:ellipsis}
  .nk-tbl .watch-btn{padding:0 2px}
}`;
  document.head.appendChild(st);
}
