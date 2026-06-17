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

-- 大口傾向タグ（moomoo確認を手動記録）buy/neutral/sell
CREATE TABLE IF NOT EXISTS flow_tags (
    code        TEXT,
    date        TEXT,        -- YYYY-MM-DD（1日1件・上書き）
    tag         TEXT,        -- buy / neutral / sell
    memo        TEXT,
    created_at  TEXT,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_ft_code ON flow_tags(code);
CREATE INDEX IF NOT EXISTS idx_ft_date ON flow_tags(date);

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
    description     TEXT,        -- 事業内容
    op_margin       REAL         -- 営業利益率%
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
    # 既存DBへの列追加（マイグレーション）
    try:
        conn.execute('ALTER TABLE fundamentals ADD COLUMN op_margin REAL')
    except Exception:
        pass
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


# 踏み上げスコアの重み（あとで調整可能）
SQUEEZE_WEIGHTS = {
    'short_delta': 8.0,   # 空売り増加(pt)
    'price_gain': 1.5,    # 期間騰落率(%)
    'short_level': 5.0,   # 現在の残高水準(%)＝燃料（重視）
    'up_days': 2.0,       # 続伸日数
    'vol': 4.0,           # 出来高急増ボーナス
}
MIN_SQUEEZE_RATIO = 2.0   # この残高割合(%)未満は踏み上げ対象外（玉が少なすぎ）


def _price_momentum(k):
    """各銘柄の直近k営業日の株価モメンタム
    返り値 code -> {c0(最新終値), gain(%), up_days, vr(出来高倍率), latest}
    """
    conn = get_conn()
    rows = conn.execute("""
        WITH ranked AS (
            SELECT code, date, close, change_pct, volume_ratio,
                   ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn
            FROM daily_prices
        )
        SELECT code,
            MAX(CASE WHEN rn=1 THEN close END)        AS c0,
            MAX(CASE WHEN rn=1 THEN date END)         AS latest,
            MAX(CASE WHEN rn=1 THEN volume_ratio END) AS vr,
            MAX(CASE WHEN rn=? THEN close END)        AS ck,
            SUM(CASE WHEN change_pct>0 THEN 1 ELSE 0 END) AS up_days
        FROM ranked WHERE rn <= ? GROUP BY code
    """, (k, k)).fetchall()
    conn.close()
    out = {}
    for r in rows:
        c0, ck = r['c0'], r['ck']
        gain = round((c0 - ck) / ck * 100, 2) if (c0 and ck and ck > 0) else None
        out[r['code']] = {'c0': c0, 'gain': gain, 'up_days': r['up_days'],
                          'vr': r['vr'], 'latest': r['latest']}
    return out


def squeeze_ranking(period='weekly', limit=50, weights=None):
    """🔥踏み上げ警戒：空売り増加 かつ 株価上昇 の銘柄を合成スコアで降順
    返り値: {'rows':[...], 'latest':短最新, 'from':基準, 'price_latest':株価最新}
    """
    w = {**SQUEEZE_WEIGHTS, **(weights or {})}
    rank = short_change_ranking(period, limit=10 ** 9, min_abs=0.01)
    inc = rank['increase']                # 空売り増加（delta_ratio>0）
    if not inc:
        return {'rows': [], 'latest': rank['latest'], 'from': rank['from'], 'price_latest': None}

    k = {'daily': 2, 'weekly': 5, 'thisweek': 5}.get(period, 5)
    mom = _price_momentum(k)
    price_latest = next((m['latest'] for m in mom.values() if m.get('latest')), None)

    rows = []
    for s in inc:
        if s['cur_ratio'] < MIN_SQUEEZE_RATIO:
            continue                       # 残高が少なすぎ＝踏ませる玉がない
        m = mom.get(s['code'])
        if not m or m['gain'] is None or m['gain'] <= 0:
            continue                       # 株価が上がっていなければ踏み上げではない
        vol_bonus = w['vol'] if (m['vr'] and m['vr'] >= 1.5) else 0
        score = (s['delta_ratio'] * w['short_delta']
                 + m['gain'] * w['price_gain']
                 + s['cur_ratio'] * w['short_level']
                 + (m['up_days'] or 0) * w['up_days']
                 + vol_bonus)
        rows.append({
            'code': s['code'], 'name': s['name'],
            'score': round(score, 1),
            'short_delta': s['delta_ratio'], 'cur_ratio': s['cur_ratio'],
            'price_gain': m['gain'], 'up_days': m['up_days'],
            'vol_ratio': round(m['vr'], 1) if m['vr'] else None,
            'close': m['c0'], 'inst': s['inst'],
        })
    rows.sort(key=lambda x: x['score'], reverse=True)
    return {'rows': rows[:limit], 'latest': rank['latest'],
            'from': rank['from'], 'price_latest': price_latest}


def short_cost_basis(code, from_date=None, to_date=None):
    """各機関の空売り単価・買戻し単価・値幅・確定損益・含み損益（Excel方式）
    from_date/to_date を渡すとその期間の売買のみで計算（エピソード自動判定は無効）
    """
    conn = get_conn()
    price_rows = conn.execute(
        'SELECT date,high,low,close FROM daily_prices WHERE code=?', (code,)).fetchall()
    short_rows = conn.execute(
        'SELECT date,institution,shares,ratio,change_shares FROM short_selling '
        'WHERE code=? ORDER BY institution,date', (code,)).fetchall()
    conn.close()
    return compute_cost_basis(price_rows, short_rows, from_date, to_date)


def compute_cost_basis(price_rows, short_rows, from_date=None, to_date=None):
    """機関別の空売り/買戻し分析（純関数・Excel方式）
    各日の増減量(change_shares)を 空売量(＋)/買戻量(−) に分け、その日の高値・安値で値付け。
      空売り単価 = Σ(空売量×高値 or 安値) / Σ空売量
      買戻し単価 = Σ(買戻量×高値 or 安値) / Σ買戻量
      値幅      = 空売り単価 − 買戻し単価（最良=高売×安戻 / 最悪=安売×高戻）
      確定損益  = 値幅 × 買戻量合計（実現）
      含み損益  = (空売り単価 − 現在株価) × 現在残高数量（残ポジの評価損益）
    返り値: {'rows':[...], 'agg':{...}, 'close':現在終値, 'latest':日付}
    """
    prices = {r['date']: r for r in price_rows}
    if not prices or not short_rows:
        return {'rows': [], 'agg': None, 'close': None, 'latest': None}

    latest = max(prices)
    close = prices[latest]['close']

    period_mode = bool(from_date or to_date)
    fd = from_date or '0000-00-00'
    td = to_date or '9999-99-99'

    by_inst = {}
    for r in short_rows:
        by_inst.setdefault(r['institution'], []).append(r)

    def hl(d):
        p = prices.get(d)
        if not p or p['high'] is None or p['low'] is None:
            return None
        return p['high'], p['low']

    rows = []
    # 全機関合算用
    g = {'sq': 0.0, 'sh': 0.0, 'sl': 0.0, 'bq': 0.0, 'bh': 0.0, 'bl': 0.0,
         'rb': 0.0, 'ra': 0.0, 'rw': 0.0, 'ub': 0.0, 'ua': 0.0, 'uw': 0.0, 'shares': 0}

    for inst, recs in by_inst.items():
        recs.sort(key=lambda x: x['date'])
        cur_shares = recs[-1]['shares'] or 0
        cur_ratio = recs[-1]['ratio'] or 0
        from datetime import datetime as _dt
        sq = sh = sl = 0.0   # 空売: 量・高値金額・安値金額
        bq = bh = bl = 0.0   # 買戻: 量・高値金額・安値金額
        prev = 0
        prev_date = None
        prev_ratio = 0.0
        ep_peak = 0.0        # 現エピソードの残高割合ピーク
        for rec in recs:
            shv = rec['shares'] or 0
            rat = rec['ratio'] or 0
            d = _dt.strptime(rec['date'], '%Y-%m-%d')
            # 期間モードでなければ、新エピソード開始でリセット
            # （初回 or 「長期空白>25日 かつ 直前0.5%未満＝報告義務消失で実質消滅」）
            if not period_mode and (prev_date is None or ((d - prev_date).days > 25 and prev_ratio < SHORT_THRESHOLD)):
                sq = sh = sl = bq = bh = bl = 0.0
                prev = 0
                ep_peak = 0.0
            prev_date = d
            chg = shv - prev          # 増減量＝残高の連続差分（Excel右表と同じ定義）
            prev = shv
            prev_ratio = rat
            ep_peak = max(ep_peak, rat)
            v = hl(rec['date'])
            if not v or chg == 0:
                continue
            # 期間モード：指定期間内の売買のみ集計
            if period_mode and not (fd <= rec['date'] <= td):
                continue
            high, low = v
            if chg > 0:
                sq += chg; sh += chg * high; sl += chg * low
            else:
                q = -chg
                bq += q; bh += q * high; bl += q * low
        # 期間モードは売りがあれば対象。通常は現エピソードで0.5%到達＆建玉残ありのみ
        if period_mode:
            if sq <= 0:
                continue
        elif sq <= 0 or cur_shares <= 0 or ep_peak < SHORT_THRESHOLD:
            continue
        sell_high, sell_low = sh / sq, sl / sq
        sell_mid = (sell_high + sell_low) / 2
        if bq > 0:
            buy_high, buy_low = bh / bq, bl / bq
            buy_mid = (buy_high + buy_low) / 2
            sp_best = sell_high - buy_low
            sp_worst = sell_low - buy_high
            sp_avg = sell_mid - buy_mid
            re_best, re_avg, re_worst = sp_best * bq, sp_avg * bq, sp_worst * bq
        else:
            buy_high = buy_low = buy_mid = None
            sp_best = sp_avg = sp_worst = None
            re_best = re_avg = re_worst = 0.0
        # 含み損益（残ポジ＝現在残高数量）。空売りは売値が現在値より高いほど益
        ub = (sell_high - close) * cur_shares
        ua = (sell_mid - close) * cur_shares
        uw = (sell_low - close) * cur_shares

        rows.append({
            'institution': inst, 'shares': int(cur_shares), 'ratio': round(cur_ratio, 2),
            'sell_high': round(sell_high, 1), 'sell_low': round(sell_low, 1), 'sell_mid': round(sell_mid, 1),
            'sell_qty': int(sq),
            'buy_high': round(buy_high, 1) if buy_high else None,
            'buy_low': round(buy_low, 1) if buy_low else None,
            'buy_mid': round(buy_mid, 1) if buy_mid else None,
            'buy_qty': int(bq),
            'spread_best': round(sp_best, 1) if sp_best is not None else None,
            'spread_avg': round(sp_avg, 1) if sp_avg is not None else None,
            'spread_worst': round(sp_worst, 1) if sp_worst is not None else None,
            'realized_best': round(re_best), 'realized_avg': round(re_avg), 'realized_worst': round(re_worst),
            'unreal_best': round(ub), 'unreal_avg': round(ua), 'unreal_worst': round(uw),
        })
        g['sq'] += sq; g['sh'] += sh; g['sl'] += sl
        g['bq'] += bq; g['bh'] += bh; g['bl'] += bl
        g['rb'] += re_best; g['ra'] += re_avg; g['rw'] += re_worst
        g['ub'] += ub; g['ua'] += ua; g['uw'] += uw; g['shares'] += cur_shares

    rows.sort(key=lambda x: x['ratio'], reverse=True)

    agg = None
    if g['sq'] > 0:
        sh_, sl_ = g['sh'] / g['sq'], g['sl'] / g['sq']
        sm_ = (sh_ + sl_) / 2
        if g['bq'] > 0:
            bh_, bl_ = g['bh'] / g['bq'], g['bl'] / g['bq']
            bm_ = (bh_ + bl_) / 2
            sp_b, sp_a, sp_w = sh_ - bl_, sm_ - bm_, sl_ - bh_
        else:
            bh_ = bl_ = bm_ = None
            sp_b = sp_a = sp_w = None
        agg = {
            # チャート帯互換（空売り単価ゾーン）
            'avg_low': round(sl_, 1), 'avg_mid': round(sm_, 1), 'avg_high': round(sh_, 1),
            'sell_high': round(sh_, 1), 'sell_low': round(sl_, 1), 'sell_mid': round(sm_, 1),
            'buy_high': round(bh_, 1) if bh_ else None,
            'buy_low': round(bl_, 1) if bl_ else None,
            'buy_mid': round(bm_, 1) if bm_ else None,
            'spread_best': round(sp_b, 1) if sp_b is not None else None,
            'spread_avg': round(sp_a, 1) if sp_a is not None else None,
            'spread_worst': round(sp_w, 1) if sp_w is not None else None,
            'realized_best': round(g['rb']), 'realized_avg': round(g['ra']), 'realized_worst': round(g['rw']),
            'unreal_best': round(g['ub']), 'unreal_avg': round(g['ua']), 'unreal_worst': round(g['uw']),
            'shares': int(g['shares']),
        }
    return {'rows': rows, 'agg': agg, 'close': close, 'latest': latest}


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


def short_gaps(lag_days=3):
    """空売りデータの欠損日を検出。
    取引日(daily_pricesに存在する日)のうち short_selling に無い日を返す。
    祝日は daily_prices に無いので自動除外。直近lag_days(T+2開示ラグ)は除く。
    """
    conn = get_conn()
    smin = conn.execute('SELECT MIN(date) d FROM short_selling').fetchone()['d']
    pmin = conn.execute('SELECT MIN(date) d FROM daily_prices').fetchone()['d']
    if not smin or not pmin:
        conn.close()
        return []
    floor = max(smin, pmin)
    trade_days = [r['date'] for r in conn.execute(
        'SELECT DISTINCT date FROM daily_prices WHERE date >= ? ORDER BY date', (floor,)
    ).fetchall()]
    short_days = set(r['date'] for r in conn.execute(
        'SELECT DISTINCT date FROM short_selling').fetchall())
    conn.close()
    eligible = trade_days[:-lag_days] if len(trade_days) > lag_days else []
    return [d for d in eligible if d not in short_days]


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
# 適時開示（TDnet）
# ============================================================
def bulk_save_disclosures(rows):
    """rows: list of dict(code,date,title,company,url,time)"""
    conn = get_conn()
    conn.executemany("""
        INSERT OR IGNORE INTO disclosures(code,date,title,category,url)
        VALUES(:code,:date,:title,:category,:url)
    """, [{'code': r['code'], 'date': r['date'], 'title': r['title'],
           'category': r.get('time', ''), 'url': r.get('url', '')} for r in rows])
    conn.commit()
    conn.close()


def get_disclosures(code, limit=20):
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM disclosures WHERE code=? ORDER BY date DESC, id DESC LIMIT ?',
        (code, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def disclosures_map(since_date):
    """since_date以降の開示を code -> [titles] で返す（ランキング材料用）"""
    conn = get_conn()
    rows = conn.execute(
        'SELECT code, date, title FROM disclosures WHERE date >= ? ORDER BY date DESC, id DESC',
        (since_date,)).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r['code'], []).append(r['title'])
    return out


def disclosure_data_range():
    conn = get_conn()
    r = conn.execute('SELECT COUNT(*) n, MAX(date) mx FROM disclosures').fetchone()
    conn.close()
    return {'n': r['n'], 'max_d': r['mx']}


def gainers_ranking(limit=100, falling=False):
    """本日上昇率ランキング（最新取引日）＋空売り比率＋TDnet材料
    falling=Trueで値下がり率
    """
    from datetime import datetime, timedelta
    conn = get_conn()
    D = conn.execute('SELECT MAX(date) d FROM daily_prices').fetchone()['d']
    if not D:
        conn.close()
        return {'date': None, 'rows': [], 'disc_date': None}
    order = 'ASC' if falling else 'DESC'
    prows = conn.execute(f"""
        SELECT dp.code, dp.close, dp.change, dp.change_pct, dp.volume, dp.volume_ratio, s.name
        FROM daily_prices dp LEFT JOIN stocks s ON dp.code=s.code
        WHERE dp.date=? AND dp.change_pct IS NOT NULL
        ORDER BY dp.change_pct {order} LIMIT ?
    """, (D, limit)).fetchall()
    conn.close()

    sd = short_max_date()
    short = short_totals_asof(sd) if sd else {}
    since = (datetime.strptime(D, '%Y-%m-%d') - timedelta(days=4)).strftime('%Y-%m-%d')
    disc = disclosures_map(since)

    conn = get_conn()
    margins = {r['code']: r['op_margin'] for r in conn.execute(
        'SELECT code, op_margin FROM fundamentals').fetchall()}
    conn.close()

    rows = []
    for r in prows:
        sv = short.get(r['code'])
        rows.append({
            'code': r['code'], 'name': r['name'] or '',
            'close': r['close'], 'change': r['change'], 'change_pct': r['change_pct'],
            'volume': r['volume'], 'volume_ratio': r['volume_ratio'],
            'short_ratio': round(sv['total_ratio'], 2) if sv else None,
            'op_margin': margins.get(r['code']),
            'materials': disc.get(r['code'], [])[:3],
        })
    return {'date': D, 'rows': rows, 'disc_date': disclosure_data_range()['max_d']}


# ============================================================
# 大口傾向タグ
# ============================================================
FLOW_TAGS = ('buy', 'neutral', 'sell')
FLOW_LABEL = {'buy': '買い優勢', 'neutral': '中立', 'sell': '売り優勢'}


def save_flow_tag(code, tag, memo='', date=None):
    if tag not in FLOW_TAGS:
        return
    conn = get_conn()
    if date is None:
        # その銘柄の最新営業日に合わせる（チャートのローソクにマーカーが乗るように）
        r = conn.execute('SELECT MAX(date) d FROM daily_prices WHERE code=?', (code,)).fetchone()
        date = (r['d'] if r and r['d'] else datetime.now().strftime('%Y-%m-%d'))
    conn.execute("""
        INSERT OR REPLACE INTO flow_tags(code,date,tag,memo,created_at)
        VALUES(?,?,?,?,?)
    """, (code, date, tag, memo, datetime.now().isoformat(timespec='seconds')))
    conn.commit()
    conn.close()


def delete_flow_tag(code, date):
    conn = get_conn()
    conn.execute('DELETE FROM flow_tags WHERE code=? AND date=?', (code, date))
    conn.commit()
    conn.close()


def get_flow_tags(code):
    conn = get_conn()
    rows = conn.execute(
        'SELECT * FROM flow_tags WHERE code=? ORDER BY date DESC', (code,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def recent_flow_tags(limit=20):
    conn = get_conn()
    rows = conn.execute("""
        SELECT ft.*, s.name FROM flow_tags ft
        LEFT JOIN stocks s ON ft.code=s.code
        ORDER BY ft.date DESC, ft.created_at DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
        (code,updated,market_cap_oku,per,pbr,eps,dividend_yield,unit_shares,description,op_margin)
        VALUES(:code,:updated,:market_cap_oku,:per,:pbr,:eps,:dividend_yield,:unit_shares,:description,:op_margin)
    """, {
        'code': d['code'], 'updated': d.get('updated'),
        'market_cap_oku': d.get('market_cap_oku'), 'per': d.get('per'),
        'pbr': d.get('pbr'), 'eps': d.get('eps'),
        'dividend_yield': d.get('dividend_yield'), 'unit_shares': d.get('unit_shares'),
        'description': d.get('description'), 'op_margin': d.get('op_margin'),
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
