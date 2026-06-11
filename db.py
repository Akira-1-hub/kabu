"""
投資データベース - SQLite スキーマ＆ヘルパー
"""
import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), 'stocks.db')


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


# ============================================================
# スキーマ
# ============================================================
SCHEMA = """
-- 銘柄マスタ（JPX）
CREATE TABLE IF NOT EXISTS stocks (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    market      TEXT,
    sector      TEXT,
    updated_at  TEXT
);

-- 日次株価（毎日蓄積）
CREATE TABLE IF NOT EXISTS daily_prices (
    code          TEXT,
    date          TEXT,        -- YYYY-MM-DD
    open          REAL,
    high          REAL,
    low           REAL,
    close         REAL,
    change        REAL,        -- 前日比
    change_pct    REAL,        -- 前日比%
    volume        INTEGER,
    avg_volume    INTEGER,     -- 25日平均出来高
    volume_ratio  REAL,        -- 出来高 / 平均
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_dp_date ON daily_prices(date);
CREATE INDEX IF NOT EXISTS idx_dp_code ON daily_prices(code);

-- 空売り残高（機関別・日付別）空売りネット形式
CREATE TABLE IF NOT EXISTS short_selling (
    code           TEXT,
    date           TEXT,        -- 計算日 YYYY-MM-DD
    institution    TEXT,        -- 空売り者（機関名）
    ratio          REAL,        -- 残高割合(%)
    change_ratio   REAL,        -- 増減率(%)
    shares         INTEGER,     -- 残高数量(株)
    change_shares  INTEGER,     -- 増減量(株)
    PRIMARY KEY (code, date, institution)
);
CREATE INDEX IF NOT EXISTS idx_ss_code ON short_selling(code);
CREATE INDEX IF NOT EXISTS idx_ss_date ON short_selling(date);

-- 適時開示・IR
CREATE TABLE IF NOT EXISTS disclosures (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    code      TEXT,
    date      TEXT,
    title     TEXT,
    category  TEXT,
    url       TEXT,
    UNIQUE(code, date, title)
);
CREATE INDEX IF NOT EXISTS idx_disc_code ON disclosures(code);

-- 条件ヒット履歴（スキャンの記録）
CREATE TABLE IF NOT EXISTS scan_hits (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT,
    date       TEXT,           -- YYYY-MM-DD
    condition  TEXT,           -- 条件名
    value      REAL,           -- 該当値（前日比%や出来高倍率）
    detail     TEXT,           -- 補足テキスト
    UNIQUE(code, date, condition)
);
CREATE INDEX IF NOT EXISTS idx_sh_code ON scan_hits(code);
CREATE INDEX IF NOT EXISTS idx_sh_date ON scan_hits(date);

-- ウォッチリスト
CREATE TABLE IF NOT EXISTS watchlist (
    code        TEXT PRIMARY KEY,
    added_date  TEXT,
    memo        TEXT
);

-- 手動メモ
CREATE TABLE IF NOT EXISTS memos (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    code    TEXT,
    date    TEXT,
    text    TEXT
);
CREATE INDEX IF NOT EXISTS idx_memo_code ON memos(code);

-- 企業ファンダメンタルズ（クリック時取得・キャッシュ）
CREATE TABLE IF NOT EXISTS fundamentals (
    code            TEXT PRIMARY KEY,
    updated         TEXT,        -- YYYY-MM-DD（この日付が今日なら再取得しない）
    market_cap_oku  REAL,        -- 時価総額（億円）
    per             REAL,        -- 予想PER
    pbr             REAL,
    eps             REAL,        -- 予想EPS
    dividend_yield  REAL,        -- 配当利回り%
    unit_shares     INTEGER,     -- 単元株数
    description     TEXT         -- 事業内容
);

-- スキャン実行ログ
CREATE TABLE IF NOT EXISTS scan_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT,
    finished_at TEXT,
    scope       TEXT,          -- watchlist / nikkei225 / all
    total       INTEGER,
    hits        INTEGER,
    settings    TEXT
);
"""


def init_db():
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


# ============================================================
# 銘柄マスタ
# ============================================================
def load_stock_master_from_csv():
    """all_codes.csv から stocks テーブルへ投入"""
    import csv
    csv_path = os.path.join(os.path.dirname(__file__), 'all_codes.csv')
    if not os.path.exists(csv_path):
        return 0
    conn = get_conn()
    now = datetime.now().isoformat(timespec='seconds')
    n = 0
    with open(csv_path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            conn.execute(
                'INSERT OR REPLACE INTO stocks(code,name,market,sector,updated_at) VALUES(?,?,?,?,?)',
                (row['code'], row['name'], row.get('market', ''), row.get('sector', ''), now)
            )
            n += 1
    conn.commit()
    conn.close()
    return n


STOCK_MARKETS = ('プライム（内国株式）', 'スタンダード（内国株式）', 'グロース（内国株式）')


def get_stock(code):
    conn = get_conn()
    r = conn.execute('SELECT * FROM stocks WHERE code=?', (code,)).fetchone()
    conn.close()
    return dict(r) if r else None


def get_stock_name(code):
    s = get_stock(code)
    return s['name'] if s else ''


def list_tradable_codes():
    """普通株式のコード一覧（ETF/REIT除外）"""
    conn = get_conn()
    rows = conn.execute(
        f"SELECT code, name FROM stocks WHERE market IN ({','.join('?'*len(STOCK_MARKETS))})",
        STOCK_MARKETS
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# 株価
# ============================================================
def save_daily_price(d: dict):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO daily_prices
        (code,date,open,high,low,close,change,change_pct,volume,avg_volume,volume_ratio)
        VALUES(:code,:date,:open,:high,:low,:close,:change,:change_pct,:volume,:avg_volume,:volume_ratio)
    """, {
        'code': d['code'], 'date': d['date'],
        'open': d.get('open'), 'high': d.get('high'), 'low': d.get('low'),
        'close': d.get('close'), 'change': d.get('change'), 'change_pct': d.get('change_pct'),
        'volume': d.get('volume'), 'avg_volume': d.get('avg_volume'),
        'volume_ratio': d.get('volume_ratio'),
    })
    conn.commit()
    conn.close()


def bulk_save_prices(rows: list[dict]):
    """履歴行の一括保存。avg_volume が None の行は既存値を壊さないよう COALESCE"""
    if not rows:
        return 0
    conn = get_conn()
    conn.executemany("""
        INSERT INTO daily_prices
        (code,date,open,high,low,close,change,change_pct,volume,avg_volume,volume_ratio)
        VALUES(:code,:date,:open,:high,:low,:close,:change,:change_pct,:volume,:avg_volume,:volume_ratio)
        ON CONFLICT(code,date) DO UPDATE SET
          open=excluded.open, high=excluded.high, low=excluded.low,
          close=excluded.close, change=excluded.change, change_pct=excluded.change_pct,
          volume=excluded.volume,
          avg_volume=COALESCE(excluded.avg_volume, daily_prices.avg_volume),
          volume_ratio=COALESCE(excluded.volume_ratio, daily_prices.volume_ratio)
    """, rows)
    conn.commit()
    conn.close()
    return len(rows)


def get_price_history(code, limit=120):
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM daily_prices WHERE code=? ORDER BY date DESC LIMIT ?',
        (code, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_latest_prices(codes=None):
    """各銘柄の最新株価を取得"""
    conn = get_conn()
    sql = """
        SELECT dp.* FROM daily_prices dp
        JOIN (SELECT code, MAX(date) md FROM daily_prices GROUP BY code) m
          ON dp.code=m.code AND dp.date=m.md
    """
    rows = conn.execute(sql).fetchall()
    conn.close()
    out = [dict(r) for r in rows]
    if codes:
        cs = set(codes)
        out = [r for r in out if r['code'] in cs]
    return out


# ============================================================
# 条件ヒット
# ============================================================
def save_scan_hit(code, date, condition, value, detail=''):
    conn = get_conn()
    conn.execute(
        'INSERT OR REPLACE INTO scan_hits(code,date,condition,value,detail) VALUES(?,?,?,?,?)',
        (code, date, condition, value, detail)
    )
    conn.commit()
    conn.close()


def get_hits_by_date(date):
    conn = get_conn()
    rows = conn.execute("""
        SELECT sh.*, s.name FROM scan_hits sh
        LEFT JOIN stocks s ON sh.code=s.code
        WHERE sh.date=? ORDER BY ABS(sh.value) DESC
    """, (date,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_hit_count_ranking(days=30):
    """直近N日で条件ヒット回数が多い銘柄ランキング"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT sh.code, s.name, COUNT(*) AS hit_count,
               MIN(sh.date) AS first_date, MAX(sh.date) AS last_date
        FROM scan_hits sh
        LEFT JOIN stocks s ON sh.code=s.code
        WHERE sh.date >= date('now', ?)
        GROUP BY sh.code
        ORDER BY hit_count DESC LIMIT 100
    """, (f'-{days} days',)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stock_hit_history(code):
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM scan_hits WHERE code=? ORDER BY date DESC',
        (code,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# ウォッチリスト
# ============================================================
def add_watch(code, memo=''):
    conn = get_conn()
    conn.execute(
        'INSERT OR REPLACE INTO watchlist(code,added_date,memo) VALUES(?,?,?)',
        (code, datetime.now().strftime('%Y-%m-%d'), memo)
    )
    conn.commit()
    conn.close()


def remove_watch(code):
    conn = get_conn()
    conn.execute('DELETE FROM watchlist WHERE code=?', (code,))
    conn.commit()
    conn.close()


def is_watched(code):
    conn = get_conn()
    r = conn.execute('SELECT 1 FROM watchlist WHERE code=?', (code,)).fetchone()
    conn.close()
    return r is not None


def get_watchlist():
    conn = get_conn()
    rows = conn.execute("""
        SELECT w.*, s.name, s.market, s.sector FROM watchlist w
        LEFT JOIN stocks s ON w.code=s.code
        ORDER BY w.added_date DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# メモ
# ============================================================
def add_memo(code, text):
    conn = get_conn()
    conn.execute(
        'INSERT INTO memos(code,date,text) VALUES(?,?,?)',
        (code, datetime.now().strftime('%Y-%m-%d %H:%M'), text)
    )
    conn.commit()
    conn.close()


def get_memos(code):
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM memos WHERE code=? ORDER BY date DESC', (code,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============================================================
# 空売り
# ============================================================
def save_short(code, date, institution, ratio, change_ratio, shares, change_shares):
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO short_selling
        (code,date,institution,ratio,change_ratio,shares,change_shares)
        VALUES(?,?,?,?,?,?,?)
    """, (code, date, institution, ratio, change_ratio, shares, change_shares))
    conn.commit()
    conn.close()


def bulk_save_short(rows):
    """rows: list of (code,date,institution,ratio,change_ratio,shares,change_shares)"""
    conn = get_conn()
    conn.executemany("""
        INSERT OR REPLACE INTO short_selling
        (code,date,institution,ratio,change_ratio,shares,change_shares)
        VALUES(?,?,?,?,?,?,?)
    """, rows)
    conn.commit()
    conn.close()
    return len(rows)


def get_short_history(code):
    """銘柄の空売り履歴（日付降順、各日の機関別）"""
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM short_selling WHERE code=? ORDER BY date DESC, ratio DESC',
        (code,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_short_daily_total(code):
    """銘柄の空売り残高推移（チャート用・繰り越し方式）
    各機関の最新報告を持ち越し、各日時点で0.5%以上の機関を合算。
    """
    from collections import OrderedDict
    conn = get_conn()
    rows = conn.execute(
        'SELECT date, institution, ratio, shares FROM short_selling WHERE code=? ORDER BY date',
        (code,)
    ).fetchall()
    conn.close()

    by_date = OrderedDict()
    for r in rows:
        by_date.setdefault(r['date'], []).append(r)

    state = {}  # institution -> (ratio, shares)
    series = []
    for date, recs in by_date.items():
        for r in recs:
            state[r['institution']] = (r['ratio'] or 0, r['shares'] or 0)
        active = [(ra, sh) for (ra, sh) in state.values() if ra >= SHORT_THRESHOLD]
        series.append({
            'date': date,
            'total_ratio': round(sum(ra for ra, _ in active), 3),
            'total_shares': sum(sh for _, sh in active),
            'institutions': len(active),
        })
    return series


def get_short_latest_by_institution(code):
    """銘柄の現在の機関別空売り残高（各機関の最新報告を繰り越し、0.5%以上）"""
    conn = get_conn()
    rows = conn.execute("""
        WITH latest_per_inst AS (
            SELECT institution, MAX(date) d FROM short_selling
            WHERE code=? GROUP BY institution
        )
        SELECT s.* FROM short_selling s
        JOIN latest_per_inst l ON s.institution=l.institution AND s.date=l.d
        WHERE s.code=? AND s.ratio >= ?
        ORDER BY s.ratio DESC
    """, (code, code, SHORT_THRESHOLD)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def short_max_date():
    conn = get_conn()
    r = conn.execute('SELECT MAX(date) d FROM short_selling').fetchone()
    conn.close()
    return r['d'] if r and r['d'] else None


# 報告義務の下限（これ未満の最新報告＝クローズ済とみなす）
SHORT_THRESHOLD = 0.5


def short_totals_asof(asof):
    """各銘柄の現在空売り残高（asof時点）
    空売りネット方式：各機関の最新報告を繰り越し、0.5%以上のものだけ合算。
    （変化のない日は報告されないため、機関ごとに最新値を持ち越す）
    """
    conn = get_conn()
    rows = conn.execute("""
        WITH latest_per_inst AS (
            SELECT code, institution, MAX(date) d
            FROM short_selling WHERE date <= ?
            GROUP BY code, institution
        )
        SELECT s.code, SUM(s.ratio) total_ratio, SUM(s.shares) total_shares,
               COUNT(*) inst, MAX(s.date) asof_date
        FROM short_selling s
        JOIN latest_per_inst l
          ON s.code=l.code AND s.institution=l.institution AND s.date=l.d
        WHERE s.ratio >= ?
        GROUP BY s.code
    """, (asof, SHORT_THRESHOLD)).fetchall()
    conn.close()
    return {r['code']: dict(r) for r in rows}


def _short_from_date(period, latest):
    """期間に応じた比較基準日を返す"""
    from datetime import datetime, timedelta
    L = datetime.strptime(latest, '%Y-%m-%d')
    if period == 'weekly':
        return (L - timedelta(days=7)).strftime('%Y-%m-%d')
    if period == 'thisweek':
        monday = L - timedelta(days=L.weekday())
        return (monday - timedelta(days=1)).strftime('%Y-%m-%d')  # 先週末基準
    return (L - timedelta(days=1)).strftime('%Y-%m-%d')  # daily


def short_active_positions_asof(asof):
    """asof時点でアクティブ(0.5%以上)な (code,institution) → 最新ポジション"""
    conn = get_conn()
    rows = conn.execute("""
        WITH latest_per_inst AS (
            SELECT code, institution, MAX(date) d
            FROM short_selling WHERE date <= ?
            GROUP BY code, institution
        )
        SELECT s.code, s.institution, s.ratio, s.shares, s.date
        FROM short_selling s
        JOIN latest_per_inst l
          ON s.code=l.code AND s.institution=l.institution AND s.date=l.d
        WHERE s.ratio >= ?
    """, (asof, SHORT_THRESHOLD)).fetchall()
    conn.close()
    return {(r['code'], r['institution']): dict(r) for r in rows}


def short_new_entries(period='daily', limit=50):
    """新規空売り：この期間に新たにアクティブ化した(code,institution)
    返り値: {'entries':[...], 'latest':date, 'from':date}
    """
    latest = short_max_date()
    if not latest:
        return {'entries': [], 'latest': None, 'from': None}
    from_date = _short_from_date(period, latest)

    now = short_active_positions_asof(latest)
    past = short_active_positions_asof(from_date)
    new_keys = set(now) - set(past)

    conn = get_conn()
    names = {r['code']: r['name'] for r in conn.execute('SELECT code,name FROM stocks').fetchall()}
    conn.close()

    entries = []
    for k in new_keys:
        d = now[k]
        entries.append({
            'code': d['code'], 'name': names.get(d['code'], ''),
            'institution': d['institution'],
            'ratio': round(d['ratio'], 3),
            'shares': int(d['shares']) if d['shares'] else 0,
            'date': d['date'],
        })
    entries.sort(key=lambda x: x['ratio'], reverse=True)
    return {'entries': entries[:limit], 'latest': latest, 'from': from_date}


def short_change_ranking(period='daily', limit=50, min_abs=0.01):
    """
    空売り残高の増減ランキング
    period: 'daily'(前日比) / 'weekly'(1週間前比) / 'thisweek'(今週頭比)
    返り値: {'increase':[...], 'decrease':[...], 'latest':date, 'from':date}
    """
    latest = short_max_date()
    if not latest:
        return {'increase': [], 'decrease': [], 'latest': None, 'from': None}

    from_date = _short_from_date(period, latest)
    cur = short_totals_asof(latest)
    past = short_totals_asof(from_date)

    # 銘柄名
    conn = get_conn()
    names = {r['code']: r['name'] for r in conn.execute('SELECT code,name FROM stocks').fetchall()}
    conn.close()

    rows = []
    for code, c in cur.items():
        p = past.get(code)
        cur_r = c['total_ratio'] or 0
        past_r = (p['total_ratio'] or 0) if p else 0
        delta = round(cur_r - past_r, 4)
        if abs(delta) < min_abs:
            continue
        cur_s = c['total_shares'] or 0
        past_s = (p['total_shares'] or 0) if p else 0
        rows.append({
            'code': code, 'name': names.get(code, ''),
            'cur_ratio': round(cur_r, 3), 'past_ratio': round(past_r, 3),
            'delta_ratio': delta,
            'cur_shares': int(cur_s), 'delta_shares': int(cur_s - past_s),
            'inst': c['inst'],
        })

    increase = sorted([r for r in rows if r['delta_ratio'] > 0],
                      key=lambda x: x['delta_ratio'], reverse=True)[:limit]
    decrease = sorted([r for r in rows if r['delta_ratio'] < 0],
                      key=lambda x: x['delta_ratio'])[:limit]
    return {'increase': increase, 'decrease': decrease, 'latest': latest, 'from': from_date}


def short_top_ratio(limit=50):
    """最新日 空売り残高割合トップ（積み上がっている銘柄）"""
    latest = short_max_date()
    if not latest:
        return []
    cur = short_totals_asof(latest)
    conn = get_conn()
    names = {r['code']: r['name'] for r in conn.execute('SELECT code,name FROM stocks').fetchall()}
    conn.close()
    rows = [{'code': c, 'name': names.get(c, ''), 'total_ratio': round(v['total_ratio'] or 0, 3),
             'total_shares': int(v['total_shares'] or 0), 'inst': v['inst'], 'date': v['asof_date']}
            for c, v in cur.items()]
    return sorted(rows, key=lambda x: x['total_ratio'], reverse=True)[:limit]


def short_data_range():
    """空売りデータの件数と日付範囲"""
    conn = get_conn()
    r = conn.execute("""
        SELECT COUNT(*) AS n, MIN(date) AS min_d, MAX(date) AS max_d,
               COUNT(DISTINCT code) AS codes, COUNT(DISTINCT date) AS days
        FROM short_selling
    """).fetchone()
    conn.close()
    return dict(r) if r else {}


def recreate_short_table():
    """空売りテーブルを作り直す（スキーマ変更時）"""
    conn = get_conn()
    conn.execute('DROP TABLE IF EXISTS short_selling')
    conn.execute("""
        CREATE TABLE short_selling (
            code TEXT, date TEXT, institution TEXT,
            ratio REAL, change_ratio REAL, shares INTEGER, change_shares INTEGER,
            PRIMARY KEY (code, date, institution)
        )
    """)
    conn.execute('CREATE INDEX idx_ss_code ON short_selling(code)')
    conn.execute('CREATE INDEX idx_ss_date ON short_selling(date)')
    conn.commit()
    conn.close()


# ============================================================
# ファンダメンタルズ
# ============================================================
def get_fundamentals(code):
    conn = get_conn()
    r = conn.execute('SELECT * FROM fundamentals WHERE code=?', (code,)).fetchone()
    conn.close()
    return dict(r) if r else None


def save_fundamentals(d: dict):
    """既存の事業内容は、新値が空なら保持する"""
    old = get_fundamentals(d['code'])
    if old and not d.get('description'):
        d['description'] = old.get('description')
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO fundamentals
        (code,updated,market_cap_oku,per,pbr,eps,dividend_yield,unit_shares,description)
        VALUES(:code,:updated,:market_cap_oku,:per,:pbr,:eps,:dividend_yield,:unit_shares,:description)
    """, {
        'code': d['code'], 'updated': d.get('updated'),
        'market_cap_oku': d.get('market_cap_oku'), 'per': d.get('per'),
        'pbr': d.get('pbr'), 'eps': d.get('eps'),
        'dividend_yield': d.get('dividend_yield'), 'unit_shares': d.get('unit_shares'),
        'description': d.get('description'),
    })
    conn.commit()
    conn.close()


# ============================================================
# スキャン実行ログ
# ============================================================
def start_scan_run(scope, settings_json):
    conn = get_conn()
    cur = conn.execute(
        'INSERT INTO scan_runs(started_at,scope,settings) VALUES(?,?,?)',
        (datetime.now().isoformat(timespec='seconds'), scope, settings_json)
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return rid


def finish_scan_run(rid, total, hits):
    conn = get_conn()
    conn.execute(
        'UPDATE scan_runs SET finished_at=?, total=?, hits=? WHERE id=?',
        (datetime.now().isoformat(timespec='seconds'), total, hits, rid)
    )
    conn.commit()
    conn.close()


def get_recent_runs(limit=10):
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?', (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == '__main__':
    init_db()
    n = load_stock_master_from_csv()
    print(f'DB初期化完了: {DB_PATH}')
    print(f'銘柄マスタ投入: {n}件')
    print(f'うち普通株式: {len(list_tradable_codes())}件')
