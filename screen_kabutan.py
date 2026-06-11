"""
前日比+3%以上 かつ 出来高急増 スクリーナー
kabutan 個別銘柄URL を日経225に対して一括チェック
"""
import requests
import pandas as pd
from io import StringIO
import time
import sys
sys.stdout.reconfigure(encoding='utf-8')

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept-Language': 'ja,en-US;q=0.9',
}

# 日経225銘柄コード（主要銘柄）
NIKKEI225 = [
    '1332','1605','1721','1762','1801','1802','1803','1808','1812','1925',
    '1928','1963','2002','2269','2282','2413','2432','2501','2502','2503',
    '2527','2579','2593','2651','2768','2801','2802','2871','2914','3086',
    '3099','3101','3289','3382','3402','3407','3436','3659','3861','3863',
    '3893','3941','4004','4005','4021','4042','4043','4061','4063','4183',
    '4188','4208','4272','4324','4452','4502','4503','4506','4507','4519',
    '4523','4543','4568','4578','4631','4689','4704','4716','4732','4751',
    '4755','4901','4902','4911','5019','5020','5101','5108','5201','5202',
    '5214','5232','5233','5301','5332','5333','5401','5406','5411','5541',
    '5631','5703','5706','5707','5711','5713','5714','5715','5801','5802',
    '5803','5901','6098','6103','6113','6178','6273','6301','6302','6305',
    '6326','6361','6367','6376','6378','6479','6501','6503','6504','6506',
    '6645','6674','6701','6702','6703','6724','6752','6753','6758','6762',
    '6770','6841','6857','6861','6902','6903','6954','6971','6976','6981',
    '7003','7004','7011','7012','7013','7201','7202','7203','7205','7211',
    '7261','7267','7269','7270','7272','7731','7733','7735','7741','7751',
    '7752','7762','7832','7911','7912','7951','8001','8002','8003','8015',
    '8031','8035','8053','8058','8233','8252','8267','8304','8306','8308',
    '8309','8316','8411','8591','8601','8604','8630','8697','8725','8750',
    '8766','8795','8830','9001','9005','9007','9008','9009','9020','9021',
    '9022','9062','9064','9101','9104','9107','9202','9301','9432','9433',
    '9434','9531','9532','9602','9613','9681','9684','9697','9735','9766',
    '9983','9984',
]


def fetch_today(code: str, sess: requests.Session) -> dict:
    """kabutan から本日の株価データを取得"""
    url = f'https://kabutan.jp/stock/kabuka?code={code}&ashi=day&page=1'
    try:
        r = sess.get(url, timeout=10)
        r.encoding = 'utf-8'
        tables = pd.read_html(StringIO(r.text))
        for t in tables:
            # 「前日比％」列を持つテーブルを探す
            if '前日比％' in t.columns and '売買高(株)' in t.columns:
                # 最新行（本日）
                row = t.iloc[0]
                try:
                    pct = float(str(row['前日比％']).replace(',', ''))
                    vol = float(str(row['売買高(株)']).replace(',', ''))
                    close = float(str(row['終値']).replace(',', '')) if '終値' in t.columns else None
                    return {'code': code, '前日比%': pct, '出来高': vol, '終値': close, 'ok': True}
                except Exception:
                    pass
    except Exception:
        pass
    return {'code': code, 'ok': False}


def calc_avg_volume(code: str, sess: requests.Session, days: int = 25) -> float:
    """過去N日の平均出来高を計算（出来高急増の基準）"""
    url = f'https://kabutan.jp/stock/kabuka?code={code}&ashi=day&page=1'
    try:
        r = sess.get(url, timeout=10)
        r.encoding = 'utf-8'
        tables = pd.read_html(StringIO(r.text))
        for t in tables:
            if '売買高(株)' in t.columns and len(t) >= 5:
                vols = pd.to_numeric(
                    t['売買高(株)'].astype(str).str.replace(',', ''),
                    errors='coerce'
                ).dropna()
                # 2行目以降（本日除く）の平均
                return vols.iloc[1:days+1].mean()
    except Exception:
        pass
    return 0.0


def screen(
    codes=None,
    min_change_pct: float = 3.0,
    volume_surge_ratio: float = 2.0,
    sleep_sec: float = 0.5,
):
    """
    スクリーニング実行
    - 前日比 >= min_change_pct%
    - 本日出来高 >= 過去25日平均 × volume_surge_ratio (出来高急増)
    """
    if codes is None:
        codes = NIKKEI225

    print(f"対象銘柄: {len(codes)}件")
    print(f"条件: 前日比 +{min_change_pct}%以上 ＋ 出来高 {volume_surge_ratio}倍以上\n")

    sess = requests.Session()
    sess.headers.update(HEADERS)

    results = []

    for i, code in enumerate(codes, 1):
        data = fetch_today(code, sess)
        if not data.get('ok'):
            continue

        pct = data['前日比%']
        if pct < min_change_pct:
            time.sleep(0.1)
            continue

        # 前日比条件クリア → 出来高確認
        avg_vol = calc_avg_volume(code, sess, days=25)
        today_vol = data['出来高']
        surge = (today_vol / avg_vol) if avg_vol > 0 else 0

        is_surge = surge >= volume_surge_ratio
        results.append({
            'コード': code,
            '終値': data['終値'],
            '前日比%': f'+{pct:.2f}%',
            '本日出来高': int(today_vol),
            '平均出来高': int(avg_vol) if avg_vol > 0 else '-',
            '出来高倍率': f'{surge:.1f}x' if avg_vol > 0 else '-',
            '出来高急増': '★' if is_surge else '',
        })

        print(f"[{i}/{len(codes)}] {code}: 前日比{pct:+.1f}% 出来高{surge:.1f}x {'★急増' if is_surge else ''}")
        time.sleep(sleep_sec)

    df = pd.DataFrame(results)
    if df.empty:
        print("\n該当銘柄なし")
        return df

    df_both = df[df['出来高急増'] == '★'].copy()
    print(f"\n{'='*60}")
    print(f"【両条件一致】前日比+{min_change_pct}%以上 ＋ 出来高急増: {len(df_both)}件")
    if not df_both.empty:
        print(df_both.to_string(index=False))

    print(f"\n【前日比+{min_change_pct}%以上】全{len(df)}件:")
    print(df.to_string(index=False))

    # Excel保存
    out = 'screen_result.xlsx'
    df.to_excel(out, index=False)
    print(f"\n→ {out} に保存しました")

    return df


if __name__ == '__main__':
    screen(min_change_pct=3.0, volume_surge_ratio=2.0)
