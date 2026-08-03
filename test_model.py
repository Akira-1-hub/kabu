"""
空売り（テスト用）：モデル（重み）とスコア計算

・重みは test_models テーブルでバージョン管理する（上書きしない）
・スコアは「正規化した特徴量 × 重み」の線形和。内訳(寄与度)を必ず残す
  → どの項目が効いたか説明できる形にしておく（仕様書13）
・本番の SQUEEZE_WEIGHTS には一切触れない
"""
import json
from datetime import datetime

import db
from test_features import FEATURE_NAMES

# 初期モデル TEST_v001 の重み。
# 本番の踏み上げスコアの考え方を出発点にしつつ、仕様書4の新項目を小さめに入れる。
# （学習が始まるまでの暫定値。学習で置き換わっていく）
INITIAL_WEIGHTS = {
    'short_ratio':  5.0,
    'short_delta':  8.0,
    'price_gain':   1.5,
    'up_days':      2.0,
    'vol_ratio':    1.0,
    'new_inst':     1.0,
    'new_ratio':    4.0,
    'dtc':          3.0,
    # --- 仕様書4の追加項目（まずは控えめに）---
    'turnover':     2.0,
    'crossing':     2.0,
    'price_resil':  0.5,
    'absorption':   0.5,
    'fuel_left':    2.0,
    'cover_amt':    0.0,   # 買い戻し量は単体では方向が定まらないので初期0
}

# 各特徴量の正規化レンジ（この範囲を0〜1に写像。外れ値の暴れを抑える）
NORM = {
    'short_ratio':  (0, 20),
    'short_delta':  (0, 3),
    'price_gain':   (-10, 20),
    'up_days':      (0, 5),
    'vol_ratio':    (0, 5),
    'new_inst':     (0, 3),
    'new_ratio':    (0, 3),
    'dtc':          (0, 15),
    'turnover':     (0, 4),
    'crossing':     (0, 1),
    'price_resil':  (-20, 40),
    'absorption':   (0, 3),
    'fuel_left':    (0, 15),
    'cover_amt':    (0, 3),
}


def normalize(name, v):
    """特徴量を0〜1に写像（範囲外は端で頭打ち）"""
    if v is None:
        return 0.0
    lo, hi = NORM.get(name, (0, 1))
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (float(v) - lo) / (hi - lo)))


def score_row(feats, weights):
    """1銘柄のスコアと寄与度の内訳を返す"""
    contrib = {}
    total = 0.0
    for name in FEATURE_NAMES:
        w = weights.get(name, 0.0)
        if not w:
            continue
        c = normalize(name, feats.get(name)) * w
        if c:
            contrib[name] = round(c, 3)
        total += c
    return round(total, 3), contrib


def confidence(row):
    """予測信頼度：データが揃っていて、極端に薄商いでないほど高い"""
    c = 1.0
    c -= 0.15 * (row.get('missing') or 0)
    f = row['feats']
    if (f.get('vol_ratio') or 0) <= 0:
        c -= 0.3
    if (f.get('short_ratio') or 0) < 1.0:
        c -= 0.2
    if not row.get('adj_close'):
        c -= 0.2
    return round(max(0.0, min(1.0, c)), 2)


# ============================================================
# モデルの読み書き
# ============================================================
def current_model():
    """現在使用中のテスト用モデル。無ければ初期モデルを作る。"""
    conn = db.get_conn()
    r = conn.execute(
        'SELECT * FROM test_models WHERE is_current=1 ORDER BY created_at DESC LIMIT 1'
    ).fetchone()
    conn.close()
    if r:
        d = dict(r)
        d['weights'] = json.loads(d['weights'])
        return d
    return create_model(INITIAL_WEIGHTS, source='manual',
                        reason='初期モデル（本番の考え方＋仕様書4の新項目を暫定値で）')


def create_model(weights, source='auto', reason='', train_from=None, train_to=None,
                 n_train=0, metrics=None, make_current=True):
    """新しいバージョンを追加する（既存は上書きしない）"""
    conn = db.get_conn()
    n = conn.execute('SELECT COUNT(*) c FROM test_models').fetchone()['c']
    version = f'TEST_v{n + 1:03d}'
    now = datetime.now().isoformat(timespec='seconds')
    today = datetime.now().strftime('%Y-%m-%d')
    if make_current:
        conn.execute('UPDATE test_models SET is_current=0, applied_to=? '
                     'WHERE is_current=1', (today,))
    conn.execute("""
        INSERT INTO test_models
        (version,created_at,applied_from,applied_to,weights,params,
         train_from,train_to,n_train,metrics,reason,is_current,source)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (version, now, today, None, json.dumps(weights, ensure_ascii=False),
          json.dumps(NORM), train_from, train_to, n_train,
          json.dumps(metrics or {}, ensure_ascii=False), reason,
          1 if make_current else 0, source))
    conn.commit()
    conn.close()
    return {'version': version, 'weights': weights, 'created_at': now,
            'is_current': 1 if make_current else 0, 'reason': reason,
            'source': source, 'n_train': n_train}


def list_models():
    conn = db.get_conn()
    rows = conn.execute(
        'SELECT * FROM test_models ORDER BY created_at DESC').fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['weights'] = json.loads(d['weights'] or '{}')
        d['metrics'] = json.loads(d['metrics'] or '{}')
        out.append(d)
    return out


# ============================================================
# 予測（ランキング）の生成
# ============================================================
def predict(asof, rows, model=None, limit=200):
    """特徴量からテスト用ランキングを作って保存する"""
    model = model or current_model()
    w = model['weights']
    scored = []
    for r in rows:
        s, contrib = score_row(r['feats'], w)
        scored.append({'code': r['code'], 'score': s, 'contrib': contrib,
                       'confidence': confidence(r)})
    scored.sort(key=lambda x: x['score'], reverse=True)

    conn = db.get_conn()
    conn.executemany("""
        INSERT OR REPLACE INTO test_predictions
        (date,code,version,rank,score,contrib,confidence) VALUES(?,?,?,?,?,?,?)
    """, [(asof, s['code'], model['version'], i + 1, s['score'],
           json.dumps(s['contrib'], ensure_ascii=False), s['confidence'])
          for i, s in enumerate(scored[:limit])])
    conn.commit()
    conn.close()
    return scored[:limit]
