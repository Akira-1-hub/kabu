/*
 自前ローソク足チャート（依存ライブラリなし・Canvas描画）
 makeStockChart(priceEl, shortEl, bars, shorts)
   bars  : [{time:'YYYY-MM-DD', open,high,low,close,volume}]  古い順
   shorts: [{time:'YYYY-MM-DD', value}]  空売り残高合計%（古い順・繰り越し済み）
 株価と空売りは同じ表示インデックスを共有 → ズーム/パンが1対1で連動
 返り値: { setDays(n) }   n<=0で全期間
*/
function makeStockChart(priceEl, shortEl, bars, shorts, marks, lines) {
  // 上昇/下落色は CSS変数(--rise/--fall)から取得 → 反転ボタンで入れ替え可能
  function cssVar(name, fb) {
    try { const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim(); return v || fb; }
    catch (e) { return fb; }
  }
  function rgba(hex, al) {
    const m = (hex || '').replace('#', '');
    if (m.length < 6) return hex;
    return `rgba(${parseInt(m.slice(0,2),16)},${parseInt(m.slice(2,4),16)},${parseInt(m.slice(4,6),16)},${al})`;
  }
  let UP = cssVar('--rise', '#ff6b6b'), DOWN = cssVar('--fall', '#4fc3f7');
  const GRID = '#20203a', AXIS = '#8888aa', CROSS = '#9aa0c0';
  const PADR = 56;                 // 右の価格軸ぶん
  const hasShort = shorts && shorts.length > 0;
  const MARKS = marks || {};       // {date: 'buy'|'neutral'|'sell'}
  const MARK_COLOR = { buy: '#2ecc71', neutral: '#95a5a6', sell: '#ff9f43' };
  let LINES = lines || [];         // [{price, color, label}] 空売り/買戻し単価など

  // 空売りを各バー日付に合わせて繰り越しアライン（同じ長さの配列に）
  const shortAligned = [];
  { let si = 0, cur = null;
    for (const b of bars) {
      while (si < shorts.length && shorts[si].time <= b.time) { cur = shorts[si].value; si++; }
      shortAligned.push(cur);
    }
  }

  // ---- Canvas生成 ----
  function setup(el, h) {
    el.style.position = 'relative';
    el.innerHTML = '';
    const cv = document.createElement('canvas');
    cv.style.width = '100%'; cv.style.height = h + 'px'; cv.style.display = 'block';
    cv.style.touchAction = 'none';
    el.appendChild(cv);
    return cv;
  }
  const cP = setup(priceEl, 340);
  const cS = hasShort ? setup(shortEl, 150) : null;

  const tip = document.createElement('div');
  tip.style.cssText = 'position:absolute;top:6px;left:6px;background:rgba(20,20,40,.85);' +
    'border:1px solid #2a2a4a;border-radius:6px;padding:5px 8px;font-size:11px;' +
    'color:#ddd;pointer-events:none;white-space:nowrap;z-index:5;display:none;line-height:1.5';
  priceEl.appendChild(tip);

  const N = bars.length;
  let a = 0, b = N - 1;     // 表示インデックス範囲（両端含む）
  let hover = null;          // クロスヘアのバーindex
  let hoverPane = null;      // 'p' or 's'
  let mouseY = 0;
  let tipX = 8, tipY = 8;    // ツールチップ位置（priceEl基準・カーソル追従）

  function dpr() { return window.devicePixelRatio || 1; }
  function fit(cv) {
    const r = cv.getBoundingClientRect();
    const d = dpr();
    cv.width = Math.max(1, Math.round(r.width * d));
    cv.height = Math.max(1, Math.round(r.height * d));
    const ctx = cv.getContext('2d');
    ctx.setTransform(d, 0, 0, d, 0, 0);
    return { ctx, w: r.width, h: r.height };
  }

  function fmtDate(s, withY) {
    const p = s.split('-');
    return withY ? `${p[0].slice(2)}/${p[1]}/${p[2]}` : `${+p[1]}/${+p[2]}`;
  }
  function fmtVol(v) {
    if (v >= 1e8) return (v / 1e8).toFixed(1) + '億';
    if (v >= 1e4) return (v / 1e4).toFixed(0) + '万';
    return String(v);
  }

  function xCenter(i, plotW) {
    const n = b - a + 1;
    return (i - a + 0.5) * (plotW / n);
  }

  // ---- 価格ペイン描画 ----
  function drawPrice() {
    const { ctx, w, h } = fit(cP);
    ctx.clearRect(0, 0, w, h);
    const plotW = w - PADR;
    const padT = 8, padB = hasShort ? 6 : 20;
    const volH = (h - padT - padB) * 0.20;
    const priceH = (h - padT - padB) - volH;
    const yTop = padT, yBot = padT + priceH;

    // 表示範囲の高安
    let lo = Infinity, hi = -Infinity, vMax = 0;
    for (let i = a; i <= b; i++) {
      lo = Math.min(lo, bars[i].low); hi = Math.max(hi, bars[i].high);
      vMax = Math.max(vMax, bars[i].volume || 0);
    }
    for (const ln of LINES) { lo = Math.min(lo, ln.price); hi = Math.max(hi, ln.price); }
    const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
    const yP = p => yTop + (1 - (p - lo) / (hi - lo)) * priceH;

    // グリッド＋価格ラベル（右）
    ctx.font = '10px sans-serif'; ctx.textBaseline = 'middle';
    ctx.strokeStyle = GRID; ctx.fillStyle = AXIS; ctx.lineWidth = 1;
    const steps = 5;
    for (let s = 0; s <= steps; s++) {
      const p = lo + (hi - lo) * s / steps, y = yP(p);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke();
      ctx.textAlign = 'left';
      ctx.fillText(p.toFixed(p < 100 ? 1 : 0), plotW + 4, y);
    }

    const n = b - a + 1;
    const bw = plotW / n;
    const cw = Math.max(1, Math.min(bw * 0.7, 14));

    // 出来高（下帯）
    const vBase = h - padB, vTop = h - padB - volH;
    for (let i = a; i <= b; i++) {
      const x = xCenter(i, plotW), bar = bars[i];
      const vh = vMax ? (bar.volume || 0) / vMax * volH : 0;
      ctx.fillStyle = (bar.close >= bar.open) ? rgba(UP, .35) : rgba(DOWN, .35);
      ctx.fillRect(x - cw / 2, vBase - vh, cw, vh);
    }

    // ローソク
    for (let i = a; i <= b; i++) {
      const bar = bars[i], x = xCenter(i, plotW);
      const up = bar.close >= bar.open;
      ctx.strokeStyle = up ? UP : DOWN; ctx.fillStyle = up ? UP : DOWN; ctx.lineWidth = 1;
      // ヒゲ
      ctx.beginPath(); ctx.moveTo(x, yP(bar.high)); ctx.lineTo(x, yP(bar.low)); ctx.stroke();
      // 実体
      const yo = yP(bar.open), yc = yP(bar.close);
      const top = Math.min(yo, yc), bh = Math.max(1, Math.abs(yc - yo));
      ctx.fillRect(x - cw / 2, top, cw, bh);
    }

    // 水平線（空売り単価/買戻し単価など）
    ctx.font = '10px sans-serif'; ctx.textAlign = 'left';
    for (const ln of LINES) {
      const y = yP(ln.price);
      ctx.strokeStyle = ln.color; ctx.setLineDash([5, 3]); ctx.lineWidth = 1.3;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = ln.color; ctx.textBaseline = 'bottom';
      ctx.fillText(ln.label + ' ' + Math.round(ln.price), 4, y - 1);
    }

    // 大口タグのマーカー（買い=緑▲下/ 売り=橙▼上 / 中立=灰●）
    for (let i = a; i <= b; i++) {
      const tg = MARKS[bars[i].time]; if (!tg) continue;
      const x = xCenter(i, plotW), col = MARK_COLOR[tg] || '#aaa';
      ctx.fillStyle = col;
      if (tg === 'buy') {
        const y = yP(bars[i].low) + 10;
        ctx.beginPath(); ctx.moveTo(x, y - 7); ctx.lineTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.closePath(); ctx.fill();
      } else if (tg === 'sell') {
        const y = yP(bars[i].high) - 10;
        ctx.beginPath(); ctx.moveTo(x, y + 7); ctx.lineTo(x - 5, y); ctx.lineTo(x + 5, y); ctx.closePath(); ctx.fill();
      } else {
        const y = yP(bars[i].high) - 10;
        ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 7); ctx.fill();
      }
    }

    drawDateAxis(ctx, plotW, h, padB, !hasShort);
    drawCross(ctx, plotW, h, padB, 'p', yP);
  }

  // ---- 空売りペイン描画 ----
  function drawShort() {
    if (!cS) return;
    const { ctx, w, h } = fit(cS);
    ctx.clearRect(0, 0, w, h);
    const plotW = w - PADR;
    const padT = 8, padB = 20;

    let lo = Infinity, hi = -Infinity, any = false;
    for (let i = a; i <= b; i++) {
      const v = shortAligned[i]; if (v == null) continue;
      any = true; lo = Math.min(lo, v); hi = Math.max(hi, v);
    }
    if (!any) { lo = 0; hi = 1; }
    lo = Math.min(lo, hi - 0.5); hi += (hi - lo) * 0.1 || 0.5;
    if (lo > 0) lo = Math.max(0, lo - (hi - lo) * 0.1);
    const yV = v => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);

    ctx.font = '10px sans-serif'; ctx.textBaseline = 'middle';
    ctx.strokeStyle = GRID; ctx.fillStyle = AXIS; ctx.lineWidth = 1;
    for (let s = 0; s <= 3; s++) {
      const v = lo + (hi - lo) * s / 3, y = yV(v);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(plotW, y); ctx.stroke();
      ctx.textAlign = 'left'; ctx.fillText(v.toFixed(1) + '%', plotW + 4, y);
    }

    // エリア＋ライン
    ctx.beginPath(); let started = false;
    for (let i = a; i <= b; i++) {
      const v = shortAligned[i]; if (v == null) continue;
      const x = xCenter(i, plotW), y = yV(v);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = UP; ctx.lineWidth = 1.5; ctx.stroke();

    drawDateAxis(ctx, plotW, h, padB, true);
    drawCross(ctx, plotW, h, padB, 's', yV);
  }

  function drawDateAxis(ctx, plotW, h, padB, withLabels) {
    const n = b - a + 1;
    const ticks = Math.min(8, n);
    ctx.fillStyle = AXIS; ctx.strokeStyle = GRID; ctx.font = '10px sans-serif';
    ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    let prevY = '';
    for (let t = 0; t <= ticks; t++) {
      const i = a + Math.round((n - 1) * t / ticks);
      if (i < a || i > b) continue;
      const x = xCenter(i, plotW);
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h - padB); ctx.strokeStyle = GRID; ctx.stroke();
      if (withLabels) {
        const y = bars[i].time.slice(0, 4);
        ctx.fillText(fmtDate(bars[i].time, y !== prevY), x, h - padB + 4);
        prevY = y;
      }
    }
  }

  function drawCross(ctx, plotW, h, padB, pane, yFn) {
    if (hover == null || hover < a || hover > b) return;
    const x = xCenter(hover, plotW);
    ctx.save();
    ctx.strokeStyle = CROSS; ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h - padB); ctx.stroke();
    if (pane === hoverPane) {
      ctx.beginPath(); ctx.moveTo(0, mouseY); ctx.lineTo(plotW, mouseY); ctx.stroke();
    }
    ctx.restore();
  }

  function draw() { drawPrice(); drawShort(); updateTip(); }

  function updateTip() {
    if (hover == null || hover < 0 || hover >= N) { tip.style.display = 'none'; return; }
    const d = bars[hover], sv = shortAligned[hover];
    const up = d.close >= d.open;
    tip.innerHTML =
      `<b>${fmtDate(d.time, true)}</b><br>` +
      `始${d.open} 高${d.high} 安${d.low} <b style="color:${up ? UP : DOWN}">終${d.close}</b><br>` +
      `出来高 ${fmtVol(d.volume || 0)}` +
      (sv != null ? ` ／ 空売り <b style="color:${UP}">${sv.toFixed(2)}%</b>` : '');
    tip.style.display = 'block';
    // カーソル近くに配置（priceEl内に収まるようクランプ）
    const pw = cP.clientWidth, ph = cP.clientHeight;
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let lx = tipX + 14, ty = tipY + 12;
    if (lx + tw > pw - 4) lx = tipX - tw - 14;   // 右が切れるなら左へ
    if (lx < 2) lx = 2;
    if (ty + th > ph - 2) ty = ph - th - 2;       // 下が切れるなら上へ
    if (ty < 2) ty = 2;
    tip.style.left = lx + 'px'; tip.style.top = ty + 'px';
  }

  // ---- 操作（ズーム/パン/クロスヘア・両ペイン共通） ----
  function idxAtX(cv, clientX) {
    const r = cv.getBoundingClientRect();
    const plotW = r.width - PADR;
    const mx = clientX - r.left;
    const n = b - a + 1;
    return { idx: Math.round(a + (mx / plotW) * n - 0.5), mx, plotW };
  }

  function zoomAt(clientX, cv, factor) {
    const { mx, plotW } = idxAtX(cv, clientX);
    const n = b - a + 1;
    let ns = Math.round(n * factor);
    ns = Math.max(10, Math.min(N, ns));
    const frac = mx / plotW;
    const anchor = a + frac * n;
    let na = Math.round(anchor - frac * ns);
    na = Math.max(0, Math.min(N - ns, na));
    a = na; b = na + ns - 1; draw();
  }

  let drag = null;
  function bind(cv, pane) {
    cv.addEventListener('wheel', e => {
      e.preventDefault();
      zoomAt(e.clientX, cv, e.deltaY > 0 ? 1.15 : 1 / 1.15);
    }, { passive: false });

    cv.addEventListener('mousedown', e => {
      drag = { x: e.clientX, a, b, cv, moved: false };
    });
    window.addEventListener('mousemove', e => {
      if (drag) {
        const r = drag.cv.getBoundingClientRect();
        const plotW = r.width - PADR, n = drag.b - drag.a + 1;
        const dBars = Math.round((e.clientX - drag.x) / (plotW / n));
        if (dBars !== 0) drag.moved = true;
        let na = drag.a - dBars;
        na = Math.max(0, Math.min(N - n, na));
        a = na; b = na + n - 1; draw();
      }
    });
    window.addEventListener('mouseup', () => { drag = null; });

    cv.addEventListener('mousemove', e => {
      const r = cv.getBoundingClientRect();
      const { idx } = idxAtX(cv, e.clientX);
      hover = Math.max(a, Math.min(b, idx));
      hoverPane = pane; mouseY = e.clientY - r.top;
      // ツールチップをカーソル位置へ（tipはpriceEl内なのでcP基準に換算）
      const pr = cP.getBoundingClientRect();
      tipX = e.clientX - pr.left;
      tipY = (pane === 'p') ? (e.clientY - pr.top) : 8;
      draw();
    });
    cv.addEventListener('mouseleave', () => { hover = null; hoverPane = null; draw(); });

    // タッチ（パン＋ピンチ）
    let tStart = null;
    cv.addEventListener('touchstart', e => {
      if (e.touches.length === 1) tStart = { mode: 'pan', x: e.touches[0].clientX, a, b };
      else if (e.touches.length === 2) {
        const d = Math.abs(e.touches[0].clientX - e.touches[1].clientX);
        tStart = { mode: 'pinch', d, a, b, cx: (e.touches[0].clientX + e.touches[1].clientX) / 2 };
      }
    }, { passive: false });
    cv.addEventListener('touchmove', e => {
      if (!tStart) return; e.preventDefault();
      if (tStart.mode === 'pan' && e.touches.length === 1) {
        const r = cv.getBoundingClientRect(), plotW = r.width - PADR, n = tStart.b - tStart.a + 1;
        const dBars = Math.round((e.touches[0].clientX - tStart.x) / (plotW / n));
        let na = tStart.a - dBars; na = Math.max(0, Math.min(N - n, na));
        a = na; b = na + n - 1; draw();
      } else if (tStart.mode === 'pinch' && e.touches.length === 2) {
        const d = Math.abs(e.touches[0].clientX - e.touches[1].clientX);
        a = tStart.a; b = tStart.b;
        zoomAt(tStart.cx, cv, d > tStart.d ? 1 / 1.04 : 1.04);
        tStart.d = d; tStart.a = a; tStart.b = b;
      }
    }, { passive: false });
    cv.addEventListener('touchend', () => { tStart = null; });
  }
  bind(cP, 'p');
  if (cS) bind(cS, 's');

  let rsto;
  window.addEventListener('resize', () => { clearTimeout(rsto); rsto = setTimeout(draw, 120); });

  function setDays(days) {
    if (days <= 0 || days >= N) { a = 0; b = N - 1; }
    else { b = N - 1; a = Math.max(0, N - days); }
    draw();
  }

  function setLines(arr) { LINES = arr || []; draw(); }
  function refreshColors() { UP = cssVar('--rise', '#ff6b6b'); DOWN = cssVar('--fall', '#4fc3f7'); draw(); }

  draw();
  return { setDays, setLines, refreshColors };
}
