"""
データ取得層 - kabutan等からスクレイピングしてDBに保存
"""
import requests
import pandas as pd
from io import StringIO
from datetime import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import db

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept-Language': 'ja,en-US;q=0.9',
}


# ============================================================
# kabutan 株価取得
# ============================================================
def fetch_kabutan(code: str, name_hint: str = '') -> dict | None:
    """kabutanから本日株価＋25日平均出来高を取得"""
    url = f'https://kabutan.jp/stock/kabuka?code={code}&ashi=day&page=1'
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        r = sess.get(url, timeout=10)
        r.encoding = 'utf-8'

        # 銘柄名（CSV優先、なければtitle）
        name = name_hint
        if not name:
            ts, te = r.text.find('<title>'), r.text.find('</title>')
            if ts >= 0 and te > ts:
                raw = r.text[ts + 7:te]
                if '【' in raw:
                    name = raw.split('【')[0].strip()

        tables = pd.read_html(StringIO(r.text))
        today_t = hist_t = None
        for t in tables:
            if '前日比％' not in t.columns or '売買高(株)' not in t.columns:
                continue
            if len(t) == 1:
                today_t = t
            elif len(t) > 1 and hist_t is None:
                hist_t = t

        src = today_t if today_t is not None else hist_t
        if src is None:
            return None

        row = src.iloc[0]

        def num(col):
            return pd.to_numeric(str(row.get(col, '')).replace(',', ''), errors='coerce')

        close = num('終値')
        chg = num('前日比')
        pct = num('前日比％')
        vol = num('売買高(株)')
        op = num('始値')
        hi = num('高値')
        lo = num('安値')

        # 日付（本日テーブルの「日付」or「本日」列）
        date_raw = str(row.get('日付', row.get('本日', ''))).strip()
        date = parse_date(date_raw)

        # 25日平均出来高
        avg_src = hist_t if hist_t is not None else src
        vols = pd.to_numeric(
            avg_src['売買高(株)'].astype(str).str.replace(',', ''), errors='coerce'
        ).dropna()
        avg_vol = vols.iloc[:25].mean() if len(vols) > 0 else vol
        ratio = (vol / avg_vol) if (avg_vol and avg_vol > 0) else None

        def f(x):
            return None if pd.isna(x) else float(x)

        def i(x):
            return None if pd.isna(x) else int(x)

        return {
            'code': code, 'name': name, 'date': date,
            'open': f(op), 'high': f(hi), 'low': f(lo), 'close': f(close),
            'change': f(chg), 'change_pct': f(pct),
            'volume': i(vol), 'avg_volume': i(avg_vol), 'volume_ratio': f(ratio),
        }
    except Exception:
        return None


def parse_date(raw: str) -> str:
    """'26/06/05' や '06/05' を YYYY-MM-DD に。失敗時は本日"""
    raw = raw.strip()
    try:
        parts = raw.replace('-', '/').split('/')
        if len(parts) == 3:
            yy, mm, dd = parts
            year = 2000 + int(yy) if len(yy) == 2 else int(yy)
            return f'{year:04d}-{int(mm):02d}-{int(dd):02d}'
        if len(parts) == 2:
            mm, dd = parts
            return f'{datetime.now().year:04d}-{int(mm):02d}-{int(dd):02d}'
    except Exception:
        pass
    return datetime.now().strftime('%Y-%m-%d')


# ============================================================
# ファンダメンタルズ（クリック時取得・キャッシュ用）
# ============================================================
def fetch_fundamentals(code: str) -> dict | None:
    """時価総額/PER/PBR/EPS/利回り=kabutan、事業内容(特色・連結事業)=Yahoo!ファイナンス"""
    import re
    from bs4 import BeautifulSoup
    from datetime import datetime as _dt

    sess = requests.Session()
    sess.headers.update(HEADERS)
    out = {'code': code, 'updated': _dt.now().strftime('%Y-%m-%d'),
           'market_cap_oku': None, 'per': None, 'pbr': None, 'eps': None,
           'dividend_yield': None, 'unit_shares': None, 'description': None}

    def fnum(s):
        s = str(s).replace(',', '').replace('倍', '').replace('％', '').replace('%', '').replace('円', '').strip()
        try:
            return float(s)
        except Exception:
            return None

    # ---- kabutanトップ：PER/PBR/利回り（thead+tbodyペア表）・時価総額・単元・EPS ----
    try:
        r = sess.get(f'https://kabutan.jp/stock/?code={code}', timeout=10)
        r.encoding = 'utf-8'
        soup = BeautifulSoup(r.text, 'lxml')

        # PER PBR 利回り 信用倍率 のヘッダ行＋値行
        for tbl in soup.find_all('table'):
            heads = [th.get_text(strip=True) for th in tbl.select('thead th')]
            if 'PER' in heads and 'PBR' in heads:
                vals = [td.get_text(strip=True) for td in tbl.select('tbody tr td')]
                pairs = dict(zip(heads, vals))
                out['per'] = fnum(pairs.get('PER'))
                out['pbr'] = fnum(pairs.get('PBR'))
                out['dividend_yield'] = fnum(pairs.get('利回り'))
                # 同じ表の下に時価総額がある
                for th in tbl.find_all('th'):
                    if '時価総額' in th.get_text(strip=True):
                        td = th.find_next('td')
                        if td:
                            m = re.search(r'(?:(\d+(?:\.\d+)?)兆)?(?:([\d,]+(?:\.\d+)?)億)?円',
                                          td.get_text(strip=True))
                            if m and (m.group(1) or m.group(2)):
                                cho = float(m.group(1)) if m.group(1) else 0.0
                                oku = float(m.group(2).replace(',', '')) if m.group(2) else 0.0
                                out['market_cap_oku'] = round(cho * 10000 + oku, 1)
                        break
                break

        # 単元株数
        for th in soup.find_all('th'):
            if '単元株数' in th.get_text(strip=True):
                td = th.find_next('td')
                if td:
                    n = fnum(td.get_text(strip=True).replace('株', ''))
                    out['unit_shares'] = int(n) if n else None
                break

        # EPS: 業績テーブル「1株益」列の予想行（先頭が「予」）のみ採用
        try:
            tables = pd.read_html(StringIO(r.text))
            for t in tables:
                cols = [str(c) for c in t.columns]
                eps_col = next((c for c in cols if '1株益' in c.replace('１', '1')), None)
                if not eps_col:
                    continue
                first = t[cols[0]].astype(str)
                pred = t[first.str.contains('^予', regex=True, na=False)]
                if len(pred):
                    v = fnum(pred.iloc[-1][eps_col])
                    if v is not None and abs(v) < 1e6:
                        out['eps'] = v
                        break
        except Exception:
            pass
        # EPSフォールバック: 株価/PER
        if out['eps'] is None and out['per'] and out['per'] > 0:
            m = re.search(r'class="kabuka"[^>]*>([\d,\.]+)', r.text)
            if m:
                price = fnum(m.group(1))
                if price:
                    out['eps'] = round(price / out['per'], 1)
    except Exception:
        return None

    # ---- Yahoo!ファイナンス profile：特色・連結事業 ----
    try:
        r2 = sess.get(f'https://finance.yahoo.co.jp/quote/{code}.T/profile', timeout=10)
        r2.encoding = 'utf-8'
        soup2 = BeautifulSoup(r2.text, 'lxml')
        parts = []
        for h2 in soup2.find_all('h2'):
            label = h2.get_text(strip=True)
            if label in ('特色', '連結事業', '単独事業'):
                p = h2.find_next('p')
                if p:
                    txt = p.get_text(' ', strip=True)
                    txt = re.sub(r'【(特色|連結事業|単独事業|海外)】', r'〔\1〕', txt)
                    parts.append(txt)
        if parts:
            out['description'] = ' ／ '.join(parts)[:600]
    except Exception:
        pass

    return out


# ============================================================
# スキャン（全銘柄取得→保存→条件判定）
# ============================================================
def scan(
    scope='all',
    min_pct=3.0,
    surge=2.0,
    mode='both',
    workers=20,
    progress_cb=None,
    stop_flag: threading.Event | None = None,
    result_sink=None,
):
    """
    scope: 'all' / 'nikkei225' / 'watchlist'
    progress_cb(done, total, count): 進捗コールバック
    result_sink: 渡すと収集中にこのリストへ逐次追加（ライブ表示用）
    返り値: 全銘柄の生データリスト
    """
    import json
    if scope == 'watchlist':
        items = [{'code': w['code'], 'name': w.get('name') or ''} for w in db.get_watchlist()]
    elif scope == 'nikkei225':
        items = [{'code': c, 'name': db.get_stock_name(c)} for c in NIKKEI225]
    else:
        items = db.list_tradable_codes()

    total = len(items)
    settings = json.dumps({'min_pct': min_pct, 'surge': surge, 'mode': mode})
    run_id = db.start_scan_run(scope, settings)

    all_results = result_sink if result_sink is not None else []   # 全銘柄の生データ
    hit_count = 0
    done = 0
    lock = threading.Lock()

    def worker(item):
        nonlocal done
        if stop_flag and stop_flag.is_set():
            return None
        d = fetch_kabutan(item['code'], item.get('name', ''))
        with lock:
            done += 1
            if progress_cb and (done % 10 == 0 or done == total):
                progress_cb(done, total, len(all_results))
        return d

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(worker, it): it for it in items}
        for fut in as_completed(futures):
            if stop_flag and stop_flag.is_set():
                ex.shutdown(wait=False, cancel_futures=True)
                break
            d = fut.result()
            if d is None:
                continue

            # DB保存（毎日蓄積）── 全銘柄の生データを保存
            db.save_daily_price(d)

            # 全件を結果に含める（フロントで後から絞り込む）
            all_results.append(d)

            # 条件判定 → 履歴(scan_hits)に記録（継続出現ランキング用）
            pct_v = d['change_pct'] or 0
            ratio_v = d['volume_ratio'] or 0
            hit_pct = abs(pct_v) >= min_pct
            hit_surge = ratio_v >= surge
            show = (hit_pct and hit_surge) if mode == 'both' else (hit_pct or hit_surge)
            if show:
                hit_count += 1
                if hit_pct:
                    db.save_scan_hit(d['code'], d['date'], '前日比変動', pct_v, f'{pct_v:+.2f}%')
                if hit_surge:
                    db.save_scan_hit(d['code'], d['date'], '出来高急増', ratio_v, f'{ratio_v:.1f}倍')

    db.finish_scan_run(run_id, total, hit_count)
    return all_results


# 日経225（scope='nikkei225'用の軽量スキャン）
NIKKEI225 = [
    '1332','1605','1721','1801','1802','1803','1808','1812','1925','1928',
    '1963','2002','2269','2282','2413','2432','2501','2502','2503','2531',
    '2768','2801','2802','2871','2914','3086','3092','3099','3101','3289',
    '3382','3401','3402','3405','3407','3436','3659','3861','3863','3402',
    '4004','4005','4021','4042','4043','4061','4063','4151','4183','4188',
    '4208','4307','4324','4385','4452','4502','4503','4506','4507','4519',
    '4523','4543','4568','4578','4661','4689','4704','4751','4755','4901',
    '4902','4911','5019','5020','5101','5108','5201','5214','5233','5301',
    '5332','5333','5401','5406','5411','5631','5706','5711','5713','5714',
    '5801','5802','5803','5831','5901','6098','6103','6113','6178','6273',
    '6301','6302','6305','6326','6361','6367','6471','6472','6473','6479',
    '6501','6503','6504','6506','6526','6594','6645','6701','6702','6723',
    '6724','6752','6753','6758','6762','6770','6841','6857','6861','6902',
    '6920','6952','6954','6971','6976','6981','6988','7003','7011','7012',
    '7013','7186','7201','7202','7203','7205','7211','7261','7267','7269',
    '7270','7272','7731','7733','7735','7741','7751','7752','7762','7832',
    '7911','7912','7951','7974','8001','8002','8015','8031','8035','8053',
    '8058','8233','8252','8267','8304','8306','8308','8309','8316','8411',
    '8591','8601','8604','8630','8697','8725','8750','8766','8795','8801',
    '8802','8804','8830','9001','9005','9007','9008','9009','9020','9021',
    '9022','9064','9101','9104','9107','9147','9201','9202','9301','9432',
    '9433','9434','9501','9502','9503','9531','9532','9602','9613','9684',
    '9697','9706','9735','9766','9843','9983','9984',
]


if __name__ == '__main__':
    db.init_db()
    print('テスト: 5銘柄スキャン...')
    test_items = [{'code': c, 'name': ''} for c in ['7203', '9984', '8035', '6758', '4502']]

    def cb(done, total, hits):
        print(f'  {done}/{total} (hits={hits})')

    # 直接fetchテスト
    for it in test_items:
        d = fetch_kabutan(it['code'])
        if d:
            print(f"  {d['code']} {d['name']}: {d['date']} 終値{d['close']} "
                  f"前日比{d['change_pct']:+.2f}% 出来高{d['volume']:,} ({d['volume_ratio']:.1f}x)")
            db.save_daily_price(d)
        else:
            print(f"  {it['code']}: 取得失敗")
