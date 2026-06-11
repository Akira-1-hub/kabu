"""
GitHub Pages公開用の静的データ(docs/data.json)を生成
PCでスキャン後にこれを実行 → push すると外から閲覧できる
"""
import json
import os
import sys
from datetime import datetime

import db

DOCS = os.path.join(os.path.dirname(__file__), 'docs')


def build():
    os.makedirs(DOCS, exist_ok=True)

    # 銘柄名マップ（上場廃止の補完込み）
    conn = db.get_conn()
    names = {r['code']: r['name'] for r in conn.execute('SELECT code,name FROM stocks').fetchall()}
    conn.close()

    # 最新株価（スキャン結果）
    prices = []
    for p in db.get_latest_prices():
        prices.append({
            'code': p['code'],
            'name': names.get(p['code'], ''),
            'date': p['date'],
            'close': p['close'],
            'change': p['change'],
            'pct': p['change_pct'],
            'vol': p['volume'],
            'ratio': p['volume_ratio'],
        })

    # 空売り（3期間）
    short = {'latest': db.short_max_date(), 'top': db.short_top_ratio(50)}
    for period in ('daily', 'weekly', 'thisweek'):
        rank = db.short_change_ranking(period, limit=50)
        new = db.short_new_entries(period, limit=50)
        short[period] = {
            'from': rank['from'],
            'increase': rank['increase'],
            'decrease': rank['decrease'],
            'new': new['entries'],
        }

    data = {
        'updated': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'prices': prices,
        'short': short,
        'hits30': db.get_hit_count_ranking(30)[:50],
    }

    out = os.path.join(DOCS, 'data.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

    size_kb = os.path.getsize(out) / 1024
    print(f'docs/data.json 生成完了: 株価{len(prices)}銘柄 / {size_kb:.0f}KB / {data["updated"]}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    build()
