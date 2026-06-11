"""
過去株価のバックフィル（Yahoo Finance一括取得版）
- kabutanはレート制限が厳しいため、過去分はyfinanceで取得
- 本日分の正確な値は通常スキャン（kabutan）が上書きする
使い方:
  python backfill_prices.py              # 全銘柄・1年分
  python backfill_prices.py --period 2y  # 2年分
  python backfill_prices.py --codes 7203 7777
"""
import argparse
import sys
import time

import pandas as pd
import yfinance as yf

import db

BATCH = 50  # 1リクエストあたりの銘柄数


def rows_from_df(code: str, sub: pd.DataFrame) -> list[dict]:
    sub = sub.dropna(subset=['Close'])
    if sub.empty:
        return []
    closes = sub['Close']
    chg = closes.diff()
    pct = closes.pct_change() * 100
    vol = sub['Volume']
    avg25 = vol.shift(1).rolling(25, min_periods=5).mean()

    rows = []
    for i, (ts, r) in enumerate(sub.iterrows()):
        v = r['Volume']
        a = avg25.iloc[i]
        rows.append({
            'code': code,
            'date': ts.strftime('%Y-%m-%d'),
            'open': round(float(r['Open']), 2) if pd.notna(r['Open']) else None,
            'high': round(float(r['High']), 2) if pd.notna(r['High']) else None,
            'low': round(float(r['Low']), 2) if pd.notna(r['Low']) else None,
            'close': round(float(r['Close']), 2),
            'change': round(float(chg.iloc[i]), 2) if pd.notna(chg.iloc[i]) else None,
            'change_pct': round(float(pct.iloc[i]), 3) if pd.notna(pct.iloc[i]) else None,
            'volume': int(v) if pd.notna(v) else None,
            'avg_volume': int(a) if pd.notna(a) else None,
            'volume_ratio': round(float(v / a), 3) if (pd.notna(v) and pd.notna(a) and a > 0) else None,
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', default='1y', help='取得期間 (1y/2y/6mo等)')
    ap.add_argument('--codes', nargs='*')
    ap.add_argument('--min-have', type=int, default=200,
                    help='既にこの日数以上ある銘柄はスキップ')
    args = ap.parse_args()

    db.init_db()
    if args.codes:
        codes = args.codes
    else:
        codes = [s['code'] for s in db.list_tradable_codes()]

    # 既に十分ある銘柄はスキップ（再実行で続きから埋まる）
    conn = db.get_conn()
    have = {r['code']: r['n'] for r in conn.execute(
        'SELECT code, COUNT(*) n FROM daily_prices GROUP BY code').fetchall()}
    conn.close()
    todo = [c for c in codes if have.get(c, 0) < args.min_have]
    print(f'対象: {len(todo)}/{len(codes)}銘柄（{args.min_have}日以上ある銘柄はスキップ） 期間={args.period}')

    t0 = time.time()
    saved = ok = 0
    for bi in range(0, len(todo), BATCH):
        batch = todo[bi:bi + BATCH]
        tickers = [f'{c}.T' for c in batch]
        try:
            df = yf.download(tickers, period=args.period, interval='1d',
                             group_by='ticker', auto_adjust=False,
                             threads=True, progress=False)
        except Exception as e:
            print(f'  batch{bi//BATCH}: download失敗 {e}')
            continue

        for c in batch:
            t = f'{c}.T'
            try:
                sub = df[t] if isinstance(df.columns, pd.MultiIndex) else df
                rows = rows_from_df(c, sub)
                if rows:
                    saved += db.bulk_save_prices(rows)
                    ok += 1
            except Exception:
                continue

        done = min(bi + BATCH, len(todo))
        el = time.time() - t0
        eta = el / done * (len(todo) - done) if done else 0
        print(f'  {done}/{len(todo)}  成功{ok}銘柄 保存{saved:,}行  経過{el/60:.1f}分 残り約{eta/60:.0f}分')
        time.sleep(0.5)

    conn = db.get_conn()
    r = conn.execute('SELECT COUNT(*) n, COUNT(DISTINCT code) c, MIN(date) mn, MAX(date) mx FROM daily_prices').fetchone()
    conn.close()
    print(f'\n完了: daily_prices 計{r["n"]:,}行 / {r["c"]}銘柄 / {r["mn"]}〜{r["mx"]} ({(time.time()-t0)/60:.1f}分)')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
