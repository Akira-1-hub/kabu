"""
空売り（テスト用）：予測後に実際に起きた株価結果を紐づける

・調整後株価(adj_close)を使う（分割・配当の断層を避けるため）
・期間内に分割等の疑いがある場合は保存しない（db.forward_returns の bad）
・市場指数(TOPIX ETF)に対する超過リターンも計算する
"""
import sys
from collections import defaultdict

import db

HIT_PCT = 10.0        # 「上昇した」とみなす閾値(%)
INDEX_SYMBOL = '1306.T'   # 超過リターンの基準（TOPIX ETF）


def _index_series():
    """基準指数の調整なし終値（ETFなので分割は稀。日付→終値）"""
    conn = db.get_conn()
    rows = conn.execute(
        'SELECT date, close FROM world_prices WHERE symbol=? ORDER BY date',
        (INDEX_SYMBOL,)).fetchall()
    conn.close()
    return [(r['date'], r['close']) for r in rows if r['close']]


def _index_returns():
    """指数の各日について k営業日後リターン(%) を返す {date: {k: pct}}"""
    ser = _index_series()
    out = {}
    for i, (d, c) in enumerate(ser):
        rec = {}
        for k in (5, 10, 20):
            j = i + k
            if j < len(ser) and c:
                rec[k] = (ser[j][1] / c - 1) * 100
        out[d] = rec
    return out


def fill(dates=None, log=print):
    """予測済みの日について、評価期間を迎えた銘柄の結果を埋める。
    dates 未指定なら test_features にあって test_outcomes が未完成の日を対象。
    """
    conn = db.get_conn()
    if dates is None:
        dates = [r['date'] for r in conn.execute("""
            SELECT DISTINCT f.date FROM test_features f ORDER BY f.date""")]
    # 対象銘柄（予測日ごと）
    targets = defaultdict(list)
    for d in dates:
        for r in conn.execute('SELECT code FROM test_features WHERE date=?', (d,)):
            targets[d].append(r['code'])

    codes = sorted({c for v in targets.values() for c in v})
    if not codes:
        conn.close()
        log('対象なし')
        return 0

    # 必要な銘柄の調整後株価をまとめて読む
    px = defaultdict(list)
    CH = 500
    for i in range(0, len(codes), CH):
        part = codes[i:i + CH]
        ph = ','.join('?' * len(part))
        for r in conn.execute(
                f'SELECT code,date,adj_close,close FROM daily_prices '
                f'WHERE code IN ({ph}) ORDER BY code,date', part):
            v = r['adj_close'] or r['close']
            if v:
                px[r['code']].append({'date': r['date'], 'adj': v})
    conn.close()

    idx = _index_returns()
    # 銘柄ごとに将来リターンを一括計算してから、予測日のぶんだけ取り出す
    fr_cache = {c: db.forward_returns(px[c]) for c in codes if px.get(c)}

    rows = []
    skipped = 0
    from datetime import datetime as _dt
    now = _dt.now().isoformat(timespec='seconds')
    for d, cs in targets.items():
        ir = idx.get(d, {})
        for c in cs:
            fr = fr_cache.get(c, {}).get(d)
            if not fr or fr.get('bad'):
                skipped += 1
                continue
            if 'r1' not in fr:      # まだ翌営業日が来ていない
                continue
            hit5 = 1 if (fr.get('mx5') is not None and fr['mx5'] >= HIT_PCT) else 0
            hit10 = 1 if (fr.get('mx10') is not None and fr['mx10'] >= HIT_PCT) else 0
            ex = {}
            for k in (5, 10, 20):
                rk, ik = fr.get(f'r{k}'), ir.get(k)
                ex[k] = round(rk - ik, 3) if (rk is not None and ik is not None) else None
            rows.append((d, c, fr.get('r1'), fr.get('r3'), fr.get('r5'),
                         fr.get('r10'), fr.get('r20'),
                         fr.get('mx5'), fr.get('mx10'), fr.get('mx20'),
                         fr.get('mn5'), fr.get('mn10'), fr.get('mn20'),
                         hit5, hit10, None, ex[5], ex[10], ex[20], now))

    if rows:
        conn = db.get_conn()
        conn.executemany("""
            INSERT OR REPLACE INTO test_outcomes
            (date,code,r1,r3,r5,r10,r20,mx5,mx10,mx20,mn5,mn10,mn20,
             hit5,hit10,days_to_hit,ex5,ex10,ex20,filled_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        conn.commit()
        conn.close()
    log(f'結果を保存: {len(rows):,}件（分割等で除外 {skipped:,}件）')
    return len(rows)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    fill()
