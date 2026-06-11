"""
JPX 空売り残高 日次Excel を自動ダウンロードしてDBに追加
ソース: https://www.jpx.co.jp/markets/public/short-selling/index.html
"""
import requests
from bs4 import BeautifulSoup
import pandas as pd
import re
import sys
from datetime import datetime

import db

INDEX_URL = 'https://www.jpx.co.jp/markets/public/short-selling/index.html'
BASE = 'https://www.jpx.co.jp'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36',
    'Accept-Language': 'ja,en;q=0.9',
}

# JPXの機関名 → 既存DB(karauri)の機関名に統一（二重カウント防止）
JPX_NAME_MAP = {
    'Barclays Capital Securities Ltd': 'Barclays Capital Securities',
    'モルガン・スタンレーMUFG証券株式会社': 'モルガン・スタンレーMUFG',
    'Nomura International plc': 'Nomura International',
    'GOLDMAN SACHS INTERNATIONAL': 'GOLDMAN SACHS',
    'MERRILL LYNCH INTERNATIONAL': 'Merrill Lynch international',
    'JPM Securities Japan Co Ltd.': 'JPモルガン証券',
    'Citigroup Global Markets Limited': 'Citigroup Global Markets ltd',
}


def normalize_institution(name: str) -> str:
    name = (name or '').strip()
    return JPX_NAME_MAP.get(name, name)


def list_jpx_files():
    """index頁から [(YYYY-MM-DD, url), ...] を取得（公表日ベース）"""
    r = requests.get(INDEX_URL, headers=HEADERS, timeout=20)
    soup = BeautifulSoup(r.text, 'lxml')
    files = []
    for a in soup.find_all('a', href=True):
        href = a['href']
        m = re.search(r'/(\d{8})_Short_Positions\.xls', href)
        if m:
            ymd = m.group(1)
            date = f'{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}'
            url = href if href.startswith('http') else BASE + href
            files.append((date, url))
    # 重複除去・新しい順
    seen = set()
    uniq = []
    for d, u in sorted(files, reverse=True):
        if d not in seen:
            seen.add(d)
            uniq.append((d, u))
    return uniq


def parse_jpx_excel(content: bytes):
    """JPX Excelをパースして行リストを返す
    返り値: list of dict(code,date,institution,ratio,shares,prev_ratio)
    """
    df = pd.read_excel(content, sheet_name=0, header=6)
    rows = []
    for t in df.itertuples(index=False):
        # col1=計算年月日 col2=コード col5=機関 col10=残高割合 col11=残高数量 col14=直近割合
        code_raw, date_raw, inst = t[2], t[1], t[5]
        # コード正規化：数値(1333.0→"1333")も英数字(264A)も対応、ヘッダ/空行は除外
        try:
            code = str(int(code_raw))
        except (ValueError, TypeError):
            code = str(code_raw).strip()
        if not re.match(r'^[0-9A-Z]{4}$', code):
            continue
        try:
            date = pd.to_datetime(date_raw).strftime('%Y-%m-%d')
        except Exception:
            continue
        if pd.isna(inst):
            continue

        def num(x):
            try:
                return float(x)
            except Exception:
                return None

        ratio = num(t[10])
        shares = num(t[11])
        prev_ratio = num(t[14])
        rows.append({
            'code': code, 'date': date, 'institution': normalize_institution(inst),
            'ratio': round(ratio * 100, 4) if ratio is not None else None,
            'shares': int(shares) if shares is not None else None,
            'prev_ratio': round(prev_ratio * 100, 4) if prev_ratio is not None else None,
        })
    return rows


def _load_prev_shares_map():
    """(code,institution) -> (date, shares) の最新マップ（増減量計算用）"""
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT s.code, s.institution, s.date, s.shares FROM short_selling s
        JOIN (SELECT code, institution, MAX(date) md FROM short_selling GROUP BY code, institution) m
          ON s.code=m.code AND s.institution=m.institution AND s.date=m.md
    """).fetchall()
    conn.close()
    return {(r['code'], r['institution']): (r['date'], r['shares']) for r in rows}


def import_jpx(since_date=None, max_files=None, log=print):
    """
    JPXの日次ファイルを取り込む
    since_date: この日付より後の計算日のみ取り込む（Noneなら現DB最大日を使用）
    """
    db.init_db()
    info = db.short_data_range()
    if since_date is None:
        since_date = info.get('max_d') or '2000-01-01'
    log(f'取り込み基準日: {since_date} より後の計算日を追加')

    files = list_jpx_files()
    if max_files:
        files = files[:max_files]
    log(f'JPX掲載ファイル: {len(files)}件 ({files[-1][0]}〜{files[0][0]})' if files else 'ファイルなし')

    prev_map = _load_prev_shares_map()
    total_added = 0
    sess = requests.Session()
    sess.headers.update(HEADERS)

    for pub_date, url in files:
        try:
            r = sess.get(url, timeout=30)
            parsed = parse_jpx_excel(r.content)
        except Exception as e:
            log(f'  {pub_date}: ダウンロード失敗 {e}')
            continue

        # since_date以降の計算日（境界日も再取込してJPXの完全版に更新）
        new_rows = [p for p in parsed if p['date'] >= since_date]
        if not new_rows:
            log(f'  公表{pub_date}: 対象計算日なし（{len(parsed)}行中0行）')
            continue

        db_rows = []
        for p in new_rows:
            key = (p['code'], p['institution'])
            change_shares = None
            if key in prev_map:
                pd_date, pd_shares = prev_map[key]
                if pd_shares is not None and p['shares'] is not None:
                    change_shares = p['shares'] - pd_shares
            change_ratio = None
            if p['ratio'] is not None and p['prev_ratio'] is not None:
                change_ratio = round(p['ratio'] - p['prev_ratio'], 4)
            db_rows.append((p['code'], p['date'], p['institution'],
                            p['ratio'], change_ratio, p['shares'], change_shares))
            prev_map[key] = (p['date'], p['shares'])

        db.bulk_save_short(db_rows)
        total_added += len(db_rows)
        log(f'  公表{pub_date}: {len(db_rows)}件追加')

    info2 = db.short_data_range()
    log(f'\n完了: {total_added}件追加  累計{info2["n"]:,}件 期間{info2["min_d"]}〜{info2["max_d"]}')
    return total_added


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    import_jpx()
