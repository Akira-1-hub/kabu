"""
空売り（テスト用）：過去分の予測を一括生成する

株価3年・空売り2.5年の履歴があるので、待たずに学習サンプルを作れる。
各予測日について「その日に見えていた情報だけ」で特徴量を作るので、
未来情報は混入しない（db.short_visible_asof が唯一の判定箇所）。

使い方:
  python test_backfill.py                    # 空売りデータのある全期間
  python test_backfill.py --from 2025-01-01
  python test_backfill.py --every 5          # 5営業日おき（試運転用）
"""
import argparse
import sys
import time

import db
import test_features as tf
import test_model as tm
import test_outcome


def trading_days(frm=None, to=None):
    conn = db.get_conn()
    sql = 'SELECT DISTINCT date FROM daily_prices'
    cond, p = [], []
    if frm:
        cond.append('date >= ?'); p.append(frm)
    if to:
        cond.append('date <= ?'); p.append(to)
    if cond:
        sql += ' WHERE ' + ' AND '.join(cond)
    rows = conn.execute(sql + ' ORDER BY date', p).fetchall()
    conn.close()
    return [r['date'] for r in rows]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='frm', default=None)
    ap.add_argument('--to', dest='to', default=None)
    ap.add_argument('--every', type=int, default=1, help='何営業日おきに作るか')
    ap.add_argument('--skip-existing', action='store_true', default=True)
    args = ap.parse_args()

    db.init_db()
    # 空売りデータが十分ある期間だけを対象にする
    conn = db.get_conn()
    smin = conn.execute('SELECT MIN(date) d FROM short_selling').fetchone()['d']
    done = {r['date'] for r in conn.execute(
        'SELECT DISTINCT date FROM test_features')}
    conn.close()

    frm = args.frm or smin
    days = trading_days(frm, args.to)[::args.every]
    if args.skip_existing:
        days = [d for d in days if d not in done]

    model = tm.current_model()
    print(f'対象 {len(days)}日 ({days[0] if days else "-"}〜{days[-1] if days else "-"}) '
          f'モデル={model["version"]}', flush=True)

    t0 = time.time()
    total = 0
    for i, d in enumerate(days, 1):
        try:
            rows = tf.build(d)
            if not rows:
                continue
            tf.save(d, rows)
            tm.predict(d, rows, model, limit=200)
            total += len(rows)
        except Exception as e:
            print(f'  {d}: 失敗 {e}', flush=True)
            continue
        if i % 10 == 0 or i == len(days):
            el = time.time() - t0
            print(f'  {i}/{len(days)}日  特徴量{total:,}件  {el:.0f}秒  '
                  f'(残り約{el / i * (len(days) - i) / 60:.0f}分)', flush=True)

    print('\n結果を紐づけ中...', flush=True)
    test_outcome.fill()
    print(f'完了 ({time.time() - t0:.0f}秒)')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
