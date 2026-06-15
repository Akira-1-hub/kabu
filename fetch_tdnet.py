"""
TDnet 適時開示を取得して disclosures テーブルに保存
ソース: https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html
"""
import requests
import re
import sys
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

import db

BASE = 'https://www.release.tdnet.info/inbs/'
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
    'Accept-Language': 'ja',
}


def fetch_tdnet_day(ymd: str) -> list[dict]:
    """指定日(YYYYMMDD)の開示を全ページ取得"""
    date = f'{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}'
    rows = []
    sess = requests.Session()
    sess.headers.update(HEADERS)
    for page in range(1, 11):  # 最大10ページ（1000件）
        url = f'{BASE}I_list_{page:03d}_{ymd}.html'
        try:
            r = sess.get(url, timeout=15)
        except Exception:
            break
        if r.status_code != 200:
            break
        r.encoding = r.apparent_encoding
        soup = BeautifulSoup(r.text, 'lxml')
        got = 0
        for tr in soup.select('table tr'):
            tds = tr.find_all('td')
            if len(tds) < 4:
                continue
            code5 = tds[1].get_text(strip=True)
            if not re.match(r'^[0-9A-Z]{5}$', code5):
                continue
            code = code5[:4]
            title = tds[3].get_text(strip=True)
            if not title:
                continue
            a = tds[3].find('a', href=True)
            url_pdf = (BASE + a['href']) if a else ''
            rows.append({
                'code': code, 'date': date,
                'time': tds[0].get_text(strip=True),
                'company': tds[2].get_text(strip=True),
                'title': title, 'url': url_pdf,
            })
            got += 1
        if got == 0:
            break
    return rows


def import_tdnet(days=2, log=print):
    """直近 days 日分のTDnet開示を取得して保存"""
    db.init_db()
    total = 0
    for i in range(days):
        d = datetime.now() - timedelta(days=i)
        ymd = d.strftime('%Y%m%d')
        rows = fetch_tdnet_day(ymd)
        if rows:
            db.bulk_save_disclosures(rows)
            total += len(rows)
        log(f'  {d.strftime("%Y-%m-%d")}: {len(rows)}件')
    log(f'TDnet取込 完了: {total}件')
    return total


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    import_tdnet()
