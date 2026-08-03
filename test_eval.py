"""
空売り（テスト用）：成績の評価

ランキングとして有効かを見る（単純な平均騰落率の最大化ではない）。
・上位N銘柄の平均リターン／最大上昇率
・スコアと将来リターンの順位相関
・上位群と下位群の差
・月ごとの安定性、銘柄集中度、業種偏り
すべて test_outcomes（実際に起きた結果）に基づく。
"""
import json
from collections import defaultdict

import db

HORIZONS = ('r5', 'r10', 'r20', 'mx5', 'mx10', 'mx20', 'ex5', 'ex10', 'ex20')


def _spearman(pairs):
    """順位相関（スコア順位 vs リターン順位）"""
    n = len(pairs)
    if n < 5:
        return None
    xs = sorted(range(n), key=lambda i: pairs[i][0])
    ys = sorted(range(n), key=lambda i: pairs[i][1])
    rx, ry = [0] * n, [0] * n
    for r, i in enumerate(xs):
        rx[i] = r
    for r, i in enumerate(ys):
        ry[i] = r
    d2 = sum((rx[i] - ry[i]) ** 2 for i in range(n))
    return round(1 - 6 * d2 / (n * (n * n - 1)), 4)


def load_joined(version, frm=None, to=None):
    """予測と結果を突き合わせて返す"""
    conn = db.get_conn()
    sql = """SELECT p.date,p.code,p.rank,p.score,f.sector,
                    o.r1,o.r3,o.r5,o.r10,o.r20,o.mx5,o.mx10,o.mx20,
                    o.mn5,o.mn10,o.mn20,o.hit5,o.hit10,o.ex5,o.ex10,o.ex20
             FROM test_predictions p
             JOIN test_outcomes o ON p.date=o.date AND p.code=o.code
             LEFT JOIN test_features f ON f.date=p.date AND f.code=p.code
             WHERE p.version=?"""
    p = [version]
    if frm:
        sql += ' AND p.date >= ?'; p.append(frm)
    if to:
        sql += ' AND p.date <= ?'; p.append(to)
    rows = [dict(r) for r in conn.execute(sql + ' ORDER BY p.date,p.rank', p)]
    conn.close()
    return rows


def evaluate(rows, main='mx5'):
    """成績をまとめる。main は主評価指標。"""
    if not rows:
        return {}
    by_date = defaultdict(list)
    for r in rows:
        by_date[r['date']].append(r)

    def avg(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    res = {'n_days': len(by_date), 'n_pred': len(rows)}

    # 上位N銘柄の成績
    for N in (5, 10, 20):
        picks = []
        for d, rs in by_date.items():
            picks += [r for r in rs if r['rank'] <= N]
        for h in HORIZONS:
            res[f'top{N}_{h}'] = avg([r[h] for r in picks])
        res[f'top{N}_win'] = round(
            100 * sum(1 for r in picks if (r['r5'] or 0) > 0) / max(1, len(picks)), 1)
        res[f'top{N}_hit5'] = round(
            100 * sum(1 for r in picks if r['hit5']) / max(1, len(picks)), 1)
        res[f'top{N}_n'] = len(picks)

    # 順位相関（日ごとに出して平均）
    for h in ('mx5', 'r5', 'r10'):
        cs = []
        for d, rs in by_date.items():
            pairs = [(-r['rank'], r[h]) for r in rs if r[h] is not None]
            c = _spearman(pairs)
            if c is not None:
                cs.append(c)
        res[f'ic_{h}'] = round(sum(cs) / len(cs), 4) if cs else None

    # 上位群 vs 下位群
    hi = [r for r in rows if r['rank'] <= 10]
    lo = [r for r in rows if r['rank'] > 50]
    if hi and lo:
        res['spread_mx5'] = round((avg([r['mx5'] for r in hi]) or 0)
                                  - (avg([r['mx5'] for r in lo]) or 0), 3)

    # 月ごとの安定性
    by_m = defaultdict(list)
    for r in rows:
        if r['rank'] <= 10:
            by_m[r['date'][:7]].append(r)
    monthly = {m: avg([r[main] for r in rs]) for m, rs in sorted(by_m.items())}
    res['monthly'] = monthly
    vals = [v for v in monthly.values() if v is not None]
    if vals:
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / len(vals)
        res['monthly_mean'] = round(mean, 3)
        res['monthly_std'] = round(var ** 0.5, 3)
        res['monthly_win'] = round(100 * sum(1 for v in vals if v > 0) / len(vals), 1)
        res['worst_month'] = round(min(vals), 3)

    # 集中度（上位10の中で同じ銘柄が占める割合）と業種偏り
    top10 = [r for r in rows if r['rank'] <= 10]
    cnt = defaultdict(int)
    for r in top10:
        cnt[r['code']] += 1
    if top10:
        res['top_code_share'] = round(100 * max(cnt.values()) / len(top10), 1)
        res['uniq_codes'] = len(cnt)
        sec = defaultdict(int)
        for r in top10:
            sec[r['sector'] or '不明'] += 1
        res['top_sector_share'] = round(100 * max(sec.values()) / len(top10), 1)

    res['main'] = main
    res['main_value'] = res.get(f'top10_{main}')
    return res


def save_evaluation(version, frm, to, kind, metrics, adopted=None, note=''):
    from datetime import datetime
    conn = db.get_conn()
    conn.execute("""INSERT INTO test_evaluations
        (evaluated_at,version,period_from,period_to,kind,metrics,adopted,note)
        VALUES(?,?,?,?,?,?,?,?)""",
        (datetime.now().isoformat(timespec='seconds'), version, frm, to, kind,
         json.dumps(metrics, ensure_ascii=False), adopted, note))
    conn.commit()
    conn.close()
