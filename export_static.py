"""
GitHub Pages公開用の静的サイトを site/ に生成
- site/index.html, detail.html  (docs/ からコピー)
- site/data.json                (一覧・ランキング)
- site/data/{code}.json         (銘柄別詳細: 株価履歴・空売り推移・機関別・企業情報)
公開は gh-pages ブランチへ force push（公開更新.bat）
"""
import json
import os
import shutil
import sys
from collections import defaultdict, OrderedDict
from datetime import datetime

import db

BASE = os.path.dirname(__file__)
DOCS = os.path.join(BASE, 'docs')
SITE = os.path.join(BASE, 'site')

PRICE_DAYS = 400     # 銘柄詳細の株価履歴（約1.5年）
SHORT_POINTS = 300   # 空売り推移の点数


def jnum(x):
    return None if x is None else (round(float(x), 4) if isinstance(x, float) else x)


def build():
    t0 = datetime.now()
    os.makedirs(os.path.join(SITE, 'data'), exist_ok=True)

    conn = db.get_conn()
    stocks = {r['code']: dict(r) for r in conn.execute('SELECT * FROM stocks').fetchall()}

    # ---- 一括ロード（高速化のためテーブルごと取得） ----
    prices_by_code = defaultdict(list)
    for r in conn.execute('SELECT * FROM daily_prices ORDER BY code, date'):
        prices_by_code[r['code']].append(r)

    shorts_by_code = defaultdict(list)
    for r in conn.execute('SELECT code,date,institution,ratio,shares,change_shares FROM short_selling ORDER BY code, date'):
        shorts_by_code[r['code']].append(r)

    hits_by_code = defaultdict(list)
    for r in conn.execute('SELECT code,date,condition,detail FROM scan_hits ORDER BY code, date DESC'):
        if len(hits_by_code[r['code']]) < 30:
            hits_by_code[r['code']].append(r)

    funds = {r['code']: dict(r) for r in conn.execute('SELECT * FROM fundamentals').fetchall()}
    conn.close()

    # ---- 一覧 data.json ----
    latest_prices = []
    for code, rows in prices_by_code.items():
        p = rows[-1]
        latest_prices.append({
            'code': code, 'name': stocks.get(code, {}).get('name', ''),
            'date': p['date'], 'close': p['close'], 'change': p['change'],
            'pct': p['change_pct'], 'vol': p['volume'], 'ratio': p['volume_ratio'],
        })

    short_sum = {'latest': db.short_max_date(), 'top': db.short_top_ratio(50)}
    for period in ('daily', 'weekly', 'thisweek'):
        rank = db.short_change_ranking(period, limit=50)
        new = db.short_new_entries(period, limit=50)
        sq = db.squeeze_ranking(period, limit=50)
        short_sum[period] = {'from': rank['from'], 'increase': rank['increase'],
                             'decrease': rank['decrease'], 'new': new['entries'],
                             'squeeze': sq['rows'], 'price_latest': sq['price_latest']}

    data = {
        'updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'prices': latest_prices,
        'short': short_sum,
        'hits30': db.get_hit_count_ranking(30)[:50],
    }
    with open(os.path.join(SITE, 'data.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

    # ---- 銘柄別 data/{code}.json ----
    all_codes = set(prices_by_code) | set(shorts_by_code)
    n_detail = 0
    for code in all_codes:
        s = stocks.get(code, {})

        # 株価履歴（直近90日）
        ph = [{'d': r['date'], 'o': r['open'], 'h': r['high'], 'l': r['low'],
               'c': r['close'], 'pct': r['change_pct'], 'v': r['volume'],
               'vr': r['volume_ratio']} for r in prices_by_code.get(code, [])[-PRICE_DAYS:]]

        # 空売り推移（繰り越し方式）＋ 機関別最新
        series = []
        state = {}
        for r in shorts_by_code.get(code, []):
            state[r['institution']] = (r['ratio'] or 0, r['shares'] or 0, r['date'],
                                       r['change_shares'])
            active = [(ra, sh) for (ra, sh, _, _) in state.values() if ra >= db.SHORT_THRESHOLD]
            series.append({'d': r['date'],
                           'r': round(sum(a for a, _ in active), 3),
                           'i': len(active)})
        # 同日まとめ（最後の値だけ残す）
        dedup = OrderedDict()
        for pt in series:
            dedup[pt['d']] = pt
        series = list(dedup.values())[-SHORT_POINTS:]

        inst = [{'institution': k, 'ratio': v[0], 'shares': v[1], 'date': v[2],
                 'chg': v[3]}
                for k, v in state.items() if v[0] >= db.SHORT_THRESHOLD]
        inst.sort(key=lambda x: x['ratio'], reverse=True)

        fund = funds.get(code)
        detail = {
            'code': code,
            'name': s.get('name', ''),
            'market': s.get('market', ''),
            'sector': s.get('sector', ''),
            'fund': {k: fund[k] for k in ('updated', 'market_cap_oku', 'per', 'pbr',
                                           'eps', 'dividend_yield', 'description')} if fund else None,
            'prices': ph,
            'short': {'series': series, 'inst': inst},
            'hits': [{'d': h['date'], 'c': h['condition'], 't': h['detail']}
                     for h in hits_by_code.get(code, [])],
        }
        with open(os.path.join(SITE, 'data', f'{code}.json'), 'w', encoding='utf-8') as f:
            json.dump(detail, f, ensure_ascii=False, separators=(',', ':'))
        n_detail += 1

    # ---- ビューア(シェル)をコピー ----
    for fn in ('index.html', 'detail.html'):
        src = os.path.join(DOCS, fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(SITE, fn))
    # チャートエンジン
    chart_src = os.path.join(BASE, 'static', 'chart.js')
    if os.path.exists(chart_src):
        shutil.copy2(chart_src, os.path.join(SITE, 'chart.js'))
    # Jekyll処理を無効化（_ファイルや高速配信のため）
    open(os.path.join(SITE, '.nojekyll'), 'w').close()

    # サイズ集計
    total = 0
    for root, _, files in os.walk(SITE):
        for fn in files:
            total += os.path.getsize(os.path.join(root, fn))
    dt = (datetime.now() - t0).total_seconds()
    print(f'site/ 生成完了: 一覧{len(latest_prices)}銘柄 / 詳細{n_detail}ファイル '
          f'/ 合計{total/1024/1024:.1f}MB / {dt:.1f}秒')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    build()
