"""
空売り（テスト用）：特徴量の抽出

予測日 T 時点で「実際に見えていた」情報だけを使って、全銘柄の特徴量を作る。
未来情報の混入を防ぐため、
  - 空売り残高は db.short_visible_asof(T) で公表済みのものだけに絞る
  - 株価は date <= T のものだけ使う
  - 時価総額など履歴の無い項目は学習特徴量に入れない（表示・層別のみ）

本番のランキングとは独立。ここでは「スコア」は作らず、素材だけを出す。
重み付けは test_model.py が行う。
"""
import json
from collections import defaultdict

import db

# 仕様書4の追加項目を含む特徴量の一覧（学習側はこの名前で参照する）
FEATURE_NAMES = [
    # --- 既存ランキング相当 ---
    'short_ratio',      # 現在の空売り残高割合(%)
    'short_delta',      # 期間中の残高増減(pt)
    'price_gain',       # 期間騰落率(%)
    'up_days',          # 続伸日数
    'vol_ratio',        # 出来高倍率
    'new_inst',         # 新規参入した機関数
    'new_ratio',        # 新規参入機関の残高合計(pt)
    'dtc',              # 買い戻し日数(残高株数÷平均出来高)
    # --- 仕様書4の追加項目 ---
    'turnover',         # 空売り回転度：増加量と減少量の絶対値合計(pt)
    'crossing',         # 機関交錯度：増加機関と減少機関が同時に存在する度合い
    'price_resil',      # 価格耐性：空売り増加に対して株価が下がっていない度合い
    'absorption',       # 売り圧吸収度：新規売り圧力が出来高にどれだけ吸収されたか
    'fuel_left',        # 残存空売り燃料：買い戻し済みを除いた残り
    'cover_amt',        # 買い戻し量(pt)
]

LOOKBACK = 7            # 期間比較の既定（暦日）
MOM_DAYS = 5            # 株価モメンタムの営業日数


def _inst_changes(conn, code_filter, frm, to, vis_sql, vis_p):
    """(code -> 機関別の残高変化) を返す。
    各機関について「期間開始時点の残高」と「期間終了時点の残高」の差を取る。
    変化のない日は報告されないため、各時点で最新値を繰り越して比較する。
    """
    def snapshot(asof):
        sql, p = db.short_visible_asof(asof)
        rows = conn.execute(f"""
            WITH v AS (SELECT * FROM short_selling WHERE date <= ? AND {sql}),
                 l AS (SELECT code, institution, MAX(date) d FROM v
                       GROUP BY code, institution)
            SELECT v.code, v.institution, v.ratio, v.shares
            FROM v JOIN l ON v.code=l.code AND v.institution=l.institution AND v.date=l.d
        """, [asof] + p).fetchall()
        out = defaultdict(dict)
        for r in rows:
            out[r['code']][r['institution']] = (r['ratio'] or 0.0, r['shares'] or 0)
        return out

    now, past = snapshot(to), snapshot(frm)
    res = {}
    for code in set(now) | set(past):
        a, b = past.get(code, {}), now.get(code, {})
        recs = []
        for inst in set(a) | set(b):
            r0, _ = a.get(inst, (0.0, 0))
            r1, s1 = b.get(inst, (0.0, 0))
            # 報告義務未満は0とみなす
            r0 = r0 if r0 >= db.SHORT_THRESHOLD else 0.0
            if r1 < db.SHORT_THRESHOLD:
                r1, s1 = 0.0, 0
            if r0 or r1:
                recs.append((inst, r0, r1, s1))
        if recs:
            res[code] = recs
    return res


def build(asof, lookback=LOOKBACK):
    """予測日 asof の全銘柄の特徴量を返す。
    返り値: [{'code':..,'feats':{...}, 'close':.., 'adj_close':.., ...}, ...]
    """
    from datetime import datetime, timedelta
    frm = (datetime.strptime(asof, '%Y-%m-%d') - timedelta(days=lookback)).strftime('%Y-%m-%d')

    conn = db.get_conn()
    vis_sql, vis_p = db.short_visible_asof(asof)

    # 実際に使える空売りデータの最終計算日（記録用）
    data_date = conn.execute(
        f'SELECT MAX(date) d FROM short_selling WHERE date <= ? AND {vis_sql}',
        [asof] + vis_p).fetchone()['d']

    # --- 株価（asof以前の直近MOM_DAYS+1日）---
    px = defaultdict(list)
    for r in conn.execute("""
        WITH r AS (SELECT code,date,close,adj_close,change_pct,volume,volume_ratio,
                          ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn
                   FROM daily_prices WHERE date <= ?)
        SELECT * FROM r WHERE rn <= ? ORDER BY code, date
    """, (asof, MOM_DAYS + 1)):
        px[r['code']].append(r)

    # --- 25日平均出来高（買い戻し日数の分母）---
    avgvol = {}
    for r in conn.execute("""
        WITH r AS (SELECT code,volume,
                          ROW_NUMBER() OVER (PARTITION BY code ORDER BY date DESC) rn
                   FROM daily_prices WHERE date <= ?)
        SELECT code, AVG(volume) v FROM r WHERE rn <= 25 AND volume IS NOT NULL GROUP BY code
    """, (asof,)):
        avgvol[r['code']] = r['v']

    # --- 機関別の残高変化 ---
    changes = _inst_changes(conn, None, frm, asof, vis_sql, vis_p)

    # --- 銘柄マスタ・時価総額（表示/層別用。学習特徴量には入れない）---
    meta = {r['code']: r for r in conn.execute(
        'SELECT code,name,sector FROM stocks')}
    caps = {r['code']: r['market_cap_oku'] for r in conn.execute(
        'SELECT code,market_cap_oku FROM fundamentals')}
    conn.close()

    out = []
    for code, recs in changes.items():
        rows = px.get(code)
        if not rows or len(rows) < 2:
            continue
        last = rows[-1]
        if last['close'] is None:
            continue

        # 残高・増減
        cur = sum(r1 for _, _, r1, _ in recs)
        prev = sum(r0 for _, r0, _, _ in recs)
        if cur < 0.5:
            continue                                  # 報告のない銘柄は対象外
        delta = cur - prev

        inc = [r1 - r0 for _, r0, r1, _ in recs if r1 > r0]
        dec = [r0 - r1 for _, r0, r1, _ in recs if r1 < r0]
        add_amt, cut_amt = sum(inc), sum(dec)

        # 空売り回転度：純増減ではなく、動いた総量
        turnover = add_amt + cut_amt
        # 機関交錯度：増える機関と減る機関が同時に存在するほど1に近い
        crossing = (min(len(inc), len(dec)) / max(1, len(inc) + len(dec))) * 2

        # 株価モメンタム
        c0 = last['adj_close'] or last['close']
        cb = rows[0]['adj_close'] or rows[0]['close']
        gain = round((c0 / cb - 1) * 100, 3) if (c0 and cb) else 0.0
        up_days = sum(1 for r in rows if (r['change_pct'] or 0) > 0)
        vr = last['volume_ratio'] or 0

        # 価格耐性：空売りが増えているのに株価が下がっていないほど高い
        price_resil = round(gain * (1 + max(0.0, add_amt)), 3)
        # 売り圧吸収度：新規売りが出来高に対してどれだけ吸収されたか
        absorption = round(add_amt / vr, 3) if vr and vr > 0 else 0.0
        # 残存燃料：買い戻し済みを差し引いた残り
        fuel_left = round(max(0.0, cur - cut_amt), 3)

        # 買い戻し日数＝現在の空売り残高株数 ÷ 25日平均出来高
        av = avgvol.get(code)
        shares_now = sum(s1 for _, _, _, s1 in recs)
        dtc = round(shares_now / av, 2) if (av and av > 0 and shares_now) else 0.0

        feats = {
            'short_ratio': round(cur, 3),
            'short_delta': round(delta, 3),
            'price_gain': gain,
            'up_days': up_days,
            'vol_ratio': round(vr, 2),
            'new_inst': sum(1 for _, r0, r1, _ in recs if r0 == 0 and r1 > 0),
            'new_ratio': round(sum(r1 for _, r0, r1, _ in recs if r0 == 0 and r1 > 0), 3),
            'dtc': dtc,
            'turnover': round(turnover, 3),
            'crossing': round(crossing, 3),
            'price_resil': price_resil,
            'absorption': absorption,
            'fuel_left': fuel_left,
            'cover_amt': round(cut_amt, 3),
        }
        missing = sum(1 for k in FEATURE_NAMES if feats.get(k) is None)
        m = meta.get(code)
        out.append({
            'code': code,
            'name': (m['name'] if m else ''),
            'sector': (m['sector'] if m else ''),
            'feats': feats,
            'close': last['close'],
            'adj_close': last['adj_close'],
            'volume': last['volume'],
            'market_cap': caps.get(code),
            'short_ratio': round(cur, 3),
            'data_date': data_date,
            'missing': missing,
        })
    return out


def save(asof, rows):
    """test_features へ保存（同じ日を作り直しても重複しない）"""
    conn = db.get_conn()
    conn.executemany("""
        INSERT OR REPLACE INTO test_features
        (date,code,feats,close,adj_close,volume,market_cap,sector,short_ratio,data_date,missing)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, [(asof, r['code'], json.dumps(r['feats'], ensure_ascii=False),
           r['close'], r['adj_close'], r['volume'], r['market_cap'],
           r['sector'], r['short_ratio'], r['data_date'], r['missing'])
          for r in rows])
    conn.commit()
    conn.close()
    return len(rows)
