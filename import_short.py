"""
空売りネット形式のExcelをDBに取り込む
列: [ID+機関] [銘柄コード] [計算日] [空売り者] [残高割合] [増減率] [残高数量] [増減量] [備考]
"""
import pandas as pd
import sys
import re

import db

sys.stdout.reconfigure(encoding='utf-8')


def parse_shares(x):
    """'225000株' -> 225000"""
    if pd.isna(x):
        return None
    s = re.sub(r'[株,\s]', '', str(x))
    try:
        return int(float(s))
    except Exception:
        return None


def parse_num(x):
    if pd.isna(x):
        return None
    try:
        return float(x)
    except Exception:
        return None


def import_excel(path, recreate=False):
    print(f'読み込み中: {path}')
    df = pd.read_excel(path, header=1)
    print(f'  総行数: {len(df):,}')

    # 列を位置で取得（列名がバラつくため）
    # 0:ID+機関  1:コード  2:計算日  3:空売り者  4:残高割合  5:増減率  6:残高数量  7:増減量  8:備考
    cols = list(df.columns)
    col_code = cols[1]
    col_date = cols[2]
    col_inst = cols[3]
    col_ratio = cols[4]
    col_chgr = cols[5]
    col_shares = cols[6]
    col_chgs = cols[7]

    if recreate:
        print('  空売りテーブルを作り直し...')
        db.recreate_short_table()

    rows = []
    skipped = 0
    for t in df.itertuples(index=False):
        code_raw = t[1]
        date_raw = t[2]
        inst = t[3]
        if pd.isna(code_raw) or pd.isna(date_raw) or pd.isna(inst):
            skipped += 1
            continue
        # コード: 1419 -> "1419"（4桁/英数字対応）
        try:
            code = str(int(code_raw))
        except Exception:
            code = str(code_raw).strip()
        # 日付 -> YYYY-MM-DD
        try:
            date = pd.to_datetime(date_raw).strftime('%Y-%m-%d')
        except Exception:
            skipped += 1
            continue
        ratio = parse_num(t[4])
        chg_ratio = parse_num(t[5])
        shares = parse_shares(t[6])
        chg_shares = parse_shares(t[7])
        # 割合は%表記に（0.0076 -> 0.76）
        if ratio is not None:
            ratio = round(ratio * 100, 4)
        if chg_ratio is not None:
            chg_ratio = round(chg_ratio * 100, 4)

        rows.append((code, date, str(inst).strip(), ratio, chg_ratio, shares, chg_shares))

    print(f'  有効行: {len(rows):,}  スキップ: {skipped:,}')
    print('  DB書き込み中...')
    # 大量データはチャンクで
    CHUNK = 20000
    for i in range(0, len(rows), CHUNK):
        db.bulk_save_short(rows[i:i + CHUNK])
        print(f'    {min(i+CHUNK, len(rows)):,}/{len(rows):,}')

    info = db.short_data_range()
    print('\n=== 取り込み完了 ===')
    print(f"  総レコード: {info['n']:,}")
    print(f"  銘柄数: {info['codes']:,}")
    print(f"  日数: {info['days']:,}")
    print(f"  期間: {info['min_d']} 〜 {info['max_d']}")


if __name__ == '__main__':
    db.init_db()
    path = sys.argv[1] if len(sys.argv) > 1 else r'C:\Users\akino\Downloads\123.xlsx'
    import_excel(path, recreate=True)
