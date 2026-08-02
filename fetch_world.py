"""
指数・海外株の日次取得（Yahoo Finance / yfinance）
- 日本の指数、米国の主要指数、為替・金利・商品
- 半導体/ハイテク中心の米国株
毎日1回まとめて取得し world_prices に積み上げる。
"""
import sys

import pandas as pd
import yfinance as yf

import db

# (シンボル, 表示名, 区分, 単位)  区分: index=指数系 / us=米国株
SYMBOLS = [
    # ---- 日本 ----
    ('^N225',   '日経平均',        'index', ''),
    ('1306.T',  'TOPIX(ETF)',      'index', ''),
    ('2516.T',  '東証グロース250', 'index', ''),
    # ---- 米国 ----
    ('^GSPC',   'S&P500',          'index', ''),
    ('^IXIC',   'ナスダック',      'index', ''),
    ('^DJI',    'NYダウ',          'index', ''),
    ('^SOX',    'SOX半導体',       'index', ''),
    ('^VIX',    'VIX恐怖指数',     'index', ''),
    # ---- 為替・金利・商品 ----
    ('JPY=X',   'ドル円',          'index', '円'),
    ('^TNX',    '米10年債利回り',  'index', '%'),
    ('GC=F',    '金',              'index', '$'),
    ('CL=F',    '原油(WTI)',       'index', '$'),
    ('BTC-USD', 'ビットコイン',    'index', '$'),
    # ---- 米国株（半導体・ハイテク） ----
    ('NVDA', 'エヌビディア',       'us', '$'),
    ('AAPL', 'アップル',           'us', '$'),
    ('MSFT', 'マイクロソフト',     'us', '$'),
    ('GOOGL', 'アルファベット',    'us', '$'),
    ('AMZN', 'アマゾン',           'us', '$'),
    ('META', 'メタ・プラットフォームズ', 'us', '$'),
    ('TSLA', 'テスラ',             'us', '$'),
    ('AVGO', 'ブロードコム',       'us', '$'),
    ('TSM',  '台湾セミコンダクター', 'us', '$'),
    ('AMD',  'アドバンスト・マイクロ・デバイセズ', 'us', '$'),
    ('INTC', 'インテル',           'us', '$'),
    ('MU',   'マイクロン',         'us', '$'),
    ('AMAT', 'アプライドマテリアルズ', 'us', '$'),
    ('LRCX', 'ラム・リサーチ',     'us', '$'),
    ('ORCL', 'オラクル',           'us', '$'),
    ('ARM',  'アーム',             'us', '$'),
    ('SNDK', 'サンディスク',       'us', '$'),
    ('SKHY', 'ＳＫハイニックス',   'us', '$'),
    ('WDC',  'ウエスタンデジタル', 'us', '$'),
]

META = {s: {'name': n, 'kind': k, 'unit': u} for s, n, k, u in SYMBOLS}


def fetch_world(period='1mo', log=print):
    """全シンボルをまとめて取得しDBへ保存。返り値: 保存件数"""
    db.init_db()
    syms = [s for s, _, _, _ in SYMBOLS]
    log(f'{len(syms)}銘柄を取得中...')
    df = yf.download(syms, period=period, interval='1d', auto_adjust=False,
                     progress=False, group_by='ticker', threads=True)
    rows = []
    ng = []
    for s in syms:
        try:
            sub = df[s].dropna(subset=['Close'])
        except Exception:
            ng.append(s)
            continue
        if sub.empty:
            ng.append(s)
            continue
        closes = sub['Close']
        chg = closes.diff()
        pct = closes.pct_change() * 100
        for i, (ts, r) in enumerate(sub.iterrows()):
            v = r.get('Volume')
            rows.append((
                s, ts.strftime('%Y-%m-%d'),
                round(float(r['Close']), 4),
                round(float(chg.iloc[i]), 4) if pd.notna(chg.iloc[i]) else None,
                round(float(pct.iloc[i]), 3) if pd.notna(pct.iloc[i]) else None,
                int(v) if v is not None and pd.notna(v) else None,
            ))
    if rows:
        db.bulk_save_world(rows)
    if ng:
        log(f'取得できず: {", ".join(ng)}')
    log(f'{len(rows)}件を保存しました')
    return len(rows)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    period = sys.argv[1] if len(sys.argv) > 1 else '1mo'
    fetch_world(period=period)
