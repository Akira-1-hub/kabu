"""
JPX公式から全上場銘柄コードを取得してキャッシュする
"""
import requests
import pandas as pd
import os, sys
sys.stdout.reconfigure(encoding='utf-8')

CACHE_FILE = os.path.join(os.path.dirname(__file__), 'all_codes.csv')

def fetch_from_jpx() -> pd.DataFrame:
    """JPX 上場銘柄一覧 Excel を取得"""
    url = 'https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls'
    print('JPXから銘柄一覧を取得中...')
    r = requests.get(url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
    r.raise_for_status()
    df = pd.read_excel(r.content, dtype={'コード': str})
    # 必要列だけ残す
    df = df[['コード', '銘柄名', '市場・商品区分', '33業種区分']].copy()
    df.columns = ['code', 'name', 'market', 'sector']
    # 4文字コード（数字のみ＋英数字コード 264A 等の新形式も含める）
    df = df[df['code'].str.match(r'^[0-9A-Z]{4}$')].reset_index(drop=True)
    return df


def load_codes(force_refresh=False) -> pd.DataFrame:
    if not force_refresh and os.path.exists(CACHE_FILE):
        df = pd.read_csv(CACHE_FILE, dtype={'code': str})
        print(f'キャッシュから読み込み: {len(df)}銘柄')
        return df
    df = fetch_from_jpx()
    df.to_csv(CACHE_FILE, index=False, encoding='utf-8-sig')
    print(f'取得完了: {len(df)}銘柄 → {CACHE_FILE}')
    return df


if __name__ == '__main__':
    df = load_codes(force_refresh=True)
    print(df.head(10))
    print(f'\n市場別:')
    print(df['market'].value_counts())
