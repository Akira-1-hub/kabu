"""
銘柄マスタにない（上場廃止等）コードの名前をkabutanから補完
"""
import requests, re, time, sys
import db

sys.stdout.reconfigure(encoding='utf-8')
HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def fetch_name(code):
    """kabutanのtitleから銘柄名を取得（廃止銘柄も可）"""
    url = f'https://kabutan.jp/stock/?code={code}'
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.encoding = 'utf-8'
        m = re.search(r'<title>(.*?)</title>', r.text, re.S)
        if not m:
            return None
        raw = m.group(1)
        if '【' in raw:
            name = raw.split('【')[0].strip()
            # 「お探しのページ」等は除外
            if name and 'お探し' not in name and 'kabutan' not in name.lower():
                return name
    except Exception:
        pass
    return None


def backfill():
    conn = db.get_conn()
    codes = [r['code'] for r in conn.execute("""
        SELECT DISTINCT s.code FROM short_selling s
        LEFT JOIN stocks st ON s.code=st.code WHERE st.code IS NULL
    """).fetchall()]
    conn.close()

    print(f'名前なしコード: {len(codes)}件 を補完中...')
    ok = 0
    for i, code in enumerate(codes, 1):
        name = fetch_name(code)
        if name:
            conn = db.get_conn()
            conn.execute(
                'INSERT OR REPLACE INTO stocks(code,name,market,sector,updated_at) VALUES(?,?,?,?,?)',
                (code, name, '(上場廃止/対象外)', '', '')
            )
            conn.commit()
            conn.close()
            ok += 1
            print(f'  [{i}/{len(codes)}] {code} → {name}')
        else:
            print(f'  [{i}/{len(codes)}] {code} → 取得不可')
        time.sleep(0.3)
    print(f'\n補完完了: {ok}/{len(codes)}件に名前を設定')


if __name__ == '__main__':
    backfill()
