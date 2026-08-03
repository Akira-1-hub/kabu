"""
調整後終値(adj_close)のバックフィル
- daily_prices.adj_close だけを更新する（close等の既存値は触らない）
- 学習・検証のリターン計算はこの列を使う

Yahooの 'Adj Close' は配当は調整するが、日本株の分割を調整しないことがある
（例 7946: 記録上の分割日は2026-03-05だが実際に価格が飛ぶのは03-02）。
そこで分割情報から「実際に価格が飛んだ日」を検出し、過去分を割り戻す。

使い方:
  python backfill_adj.py                 # 全銘柄・3年
  python backfill_adj.py --codes 7946 8227
"""
import argparse
import sys
import time

import pandas as pd
import yfinance as yf

import db

BATCH = 50
SEARCH = 10          # 記録上の分割日から前後何営業日まで実際の断層を探すか


def split_factors(sub: pd.DataFrame) -> pd.Series:
    """各日について「その日より後に起きた分割倍率の累積」を返す。
    過去の株価をこれで割ると、現在の株数ベースに揃う。
    """
    close = sub['Close'].dropna()
    idx = close.index
    factor = pd.Series(1.0, index=idx)
    if 'Stock Splits' not in sub.columns:
        return factor
    splits = sub['Stock Splits']
    splits = splits[splits.fillna(0) != 0]
    if splits.empty:
        return factor

    ratios = close / close.shift(1)
    for ts, ratio in splits.items():
        ratio = float(ratio)
        if ratio <= 0:
            continue
        # 記録日の前後で「価格が 1/ratio 倍に飛んだ日」を探す（そこが実際の権利落ち日）
        target = 1.0 / ratio
        lo, hi = ts - pd.Timedelta(days=SEARCH * 2), ts + pd.Timedelta(days=SEARCH * 2)
        cand = ratios[(ratios.index >= lo) & (ratios.index <= hi)].dropna()
        if not len(cand):
            continue
        diff = (cand - target).abs()
        if float(diff.min()) >= target * 0.25:
            continue      # 断層が見つからない＝既に調整済みの系列。触らない（二重調整の防止）
        # 権利落ち日より前の日を割り戻す
        factor[factor.index < diff.idxmin()] *= ratio
    return factor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', default='3y')
    ap.add_argument('--codes', nargs='*')
    ap.add_argument('--only-missing', action='store_true')
    args = ap.parse_args()

    db.init_db()
    codes = args.codes or [s['code'] for s in db.list_tradable_codes()]
    if args.only_missing:
        conn = db.get_conn()
        done = {r['code'] for r in conn.execute(
            'SELECT code FROM daily_prices WHERE adj_close IS NOT NULL '
            'GROUP BY code HAVING COUNT(*) > 50')}
        conn.close()
        codes = [c for c in codes if c not in done]

    print(f'対象 {len(codes)}銘柄 期間={args.period}', flush=True)
    t0 = time.time()
    updated = ok = ng = n_split = 0

    for bi in range(0, len(codes), BATCH):
        batch = codes[bi:bi + BATCH]
        try:
            df = yf.download([f'{c}.T' for c in batch], period=args.period,
                             interval='1d', group_by='ticker', auto_adjust=False,
                             actions=True, threads=True, progress=False)
        except Exception as e:
            print(f'  batch{bi // BATCH}: 失敗 {e}', flush=True)
            ng += len(batch)
            continue

        rows = []
        for c in batch:
            try:
                sub = df[f'{c}.T'] if isinstance(df.columns, pd.MultiIndex) else df
                sub = sub.dropna(subset=['Close'])
                if sub.empty:
                    ng += 1
                    continue
                base = sub['Adj Close'] if 'Adj Close' in sub.columns else sub['Close']
                fac = split_factors(sub)
                if (fac != 1.0).any():
                    n_split += 1
                adj = (base / fac).dropna()
                for ts, v in adj.items():
                    rows.append((round(float(v), 4), c, ts.strftime('%Y-%m-%d')))
                ok += 1
            except Exception:
                ng += 1

        if rows:
            conn = db.get_conn()
            conn.executemany(
                'UPDATE daily_prices SET adj_close=? WHERE code=? AND date=?', rows)
            conn.commit()
            conn.close()
            updated += len(rows)

        done = min(bi + BATCH, len(codes))
        print(f'  {done}/{len(codes)}銘柄  更新{updated:,}行  分割調整{n_split}銘柄  '
              f'{time.time() - t0:.0f}秒', flush=True)

    print(f'\n完了: {ok}銘柄OK / {ng}銘柄NG / {updated:,}行更新 / '
          f'分割調整{n_split}銘柄 ({time.time() - t0:.0f}秒)')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
