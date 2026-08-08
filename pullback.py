"""
上昇後・調整終了候補ランキング

「一度強く上昇 → 調整入り → 売り圧力と出来高が縮小 → 値動きが収束」
という段階の銘柄を拾う。反転（高値突破・陽線）は必須にせず、
起きていれば「反転確認済み」のフラグを立てるだけにする。
（反転を必須にすると発見が遅れるため）

空売り残高・空売り増減は一切使わない。既存の踏み上げ系ランキングとは独立。
"""
import db

WINDOW = 30          # 何営業日さかのぼって「上昇→調整」を探すか
MIN_BARS = 22        # これ未満しか株価が無い銘柄は対象外
MIN_RISE = 5.0       # 直前上昇率がこれ未満なら対象外（そもそも上げていない）
RECENT = 3           # 「直近」とみなす営業日数

# 100点の配分（仕様どおり）
WEIGHTS = {
    'rise':      20,   # 直前上昇の強さ
    'drop':      15,   # 高値からの調整位置
    'vol_dry':   25,   # 調整中の出来高減少（最重視）
    'body':      15,   # ローソク足実体の縮小
    'decay':     15,   # 下落圧力の減衰
    'settle':    10,   # 安値更新停止・値動き収束
}

LABEL = {
    'rise':    ('直前上昇', '直近30日の安値から高値までの上昇率。上げていない銘柄は対象外。'),
    'drop':    ('調整位置', '高値からの下落率。浅すぎ（まだ調整前）も深すぎ（崩壊）も減点。'),
    'vol_dry': ('出来高減少', '直近3日の出来高 ÷ 上昇局面の出来高。減っているほど高得点。'),
    'body':    ('実体縮小', 'ローソク足の実体｜終値-始値｜÷値幅。小さいほど値動きが収束。'),
    'decay':   ('下落圧力減衰', '調整の前半と後半を比べ、下げ幅と陰線の出来高が縮んでいるか。'),
    'settle':  ('安値更新停止', '最安値をつけてからの経過日数と、値幅の縮小具合。'),
}


def _clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))


def _ramp(v, lo, hi):
    """lo以下で0、hi以上で1になる直線"""
    if hi == lo:
        return 0.0
    return _clamp((v - lo) / (hi - lo))


def _score_rise(rise):
    # 5%で0.2、25%以上で満点
    if rise < MIN_RISE:
        return 0.0
    return _clamp(0.2 + 0.8 * _ramp(rise, MIN_RISE, 25.0))


def _score_drop(drop):
    # 浅すぎ（まだ調整していない）と深すぎ（崩壊）を減点し、15〜30%を最良とする
    if drop < 5:
        return _ramp(drop, 0, 5) * 0.25
    if drop < 15:
        return 0.25 + 0.75 * _ramp(drop, 5, 15)
    if drop <= 30:
        return 1.0
    if drop <= 45:
        return 1.0 - 0.85 * _ramp(drop, 30, 45)
    return 0.1


def _score_vol(ratio):
    # 上昇局面の3割以下まで枯れていれば満点、同水準以上なら0
    if ratio is None:
        return 0.0
    if ratio <= 0.3:
        return 1.0
    return _clamp(1.0 - _ramp(ratio, 0.3, 1.0))


def _score_body(body):
    # 実体が値幅の15%以下なら満点、60%以上で0
    if body is None:
        return 0.0
    return _clamp(1.0 - _ramp(body, 0.15, 0.60))


def compute(bars):
    """1銘柄ぶんの評価。bars は古い順の [{d,o,h,l,c,v}]（調整済み）"""
    n = len(bars)
    if n < MIN_BARS:
        return None
    w = bars[-WINDOW:] if n > WINDOW else bars
    m = len(w)

    lows = [b['l'] for b in w]
    highs = [b['h'] for b in w]

    # 安値 → その後の高値、という順序で「上昇」を探す
    il = min(range(m - 3), key=lambda i: lows[i])          # 高値を取る余地を残す
    ih = max(range(il + 1, m), key=lambda i: highs[i])
    if ih >= m - 1:
        return None                                        # 高値が直近すぎて調整がない
    low, high = lows[il], highs[ih]
    if not low or not high or low <= 0:
        return None

    rise = (high / low - 1) * 100
    if rise < MIN_RISE:
        return None

    last = w[-1]
    drop = (1 - last['c'] / high) * 100 if high else 0.0

    # 出来高：上昇局面（安値〜高値）と直近を比べる
    up_vols = [b['v'] for b in w[il:ih + 1] if b['v']]
    rec_vols = [b['v'] for b in w[-RECENT:] if b['v']]
    up_v = sum(up_vols) / len(up_vols) if up_vols else None
    rec_v = sum(rec_vols) / len(rec_vols) if rec_vols else None
    volr = (rec_v / up_v) if (up_v and rec_v and up_v > 0) else None

    # ローソク足の実体率（直近3日平均）
    bodies = []
    for b in w[-RECENT:]:
        rng = (b['h'] - b['l'])
        if rng and rng > 0:
            bodies.append(abs(b['c'] - b['o']) / rng)
    body = sum(bodies) / len(bodies) if bodies else None

    # 調整局面（高値の翌日以降）を前半・後半に分けて、下げ圧力の変化を見る
    pb = w[ih + 1:]
    decay = 0.0
    if len(pb) >= 4:
        half = len(pb) // 2
        early, late = pb[:half], pb[half:]

        def down_stats(seg):
            mags, vols = [], []
            for i, b in enumerate(seg):
                prev = seg[i - 1]['c'] if i else None
                if prev and b['c'] < prev:
                    mags.append((1 - b['c'] / prev) * 100)
                    if b['v']:
                        vols.append(b['v'])
            return (sum(mags) / len(mags) if mags else 0.0,
                    sum(vols) / len(vols) if vols else 0.0)

        me, ve = down_stats(early)
        ml, vl = down_stats(late)
        s_mag = 1.0 if me <= 0 else _clamp(1.0 - ml / me)
        s_vol = 1.0 if ve <= 0 else _clamp(1.0 - vl / ve)
        decay = 0.6 * s_mag + 0.4 * s_vol

    # 安値更新の停止と値幅の収束
    settle = 0.0
    if pb:
        pl = [b['l'] for b in pb]
        since = len(pb) - 1 - min(range(len(pb)), key=lambda i: pl[i])
        s_since = _ramp(since, 0, 5)                       # 5日以上更新なしで満点
        rngs = [(b['h'] - b['l']) / b['c'] for b in w if b['c']]
        s_rng = 0.0
        if len(rngs) >= 6:
            rec_r = sum(rngs[-RECENT:]) / RECENT
            base_r = sum(rngs[:-RECENT]) / max(1, len(rngs) - RECENT)
            if base_r > 0:
                s_rng = _clamp(1.0 - rec_r / base_r)
        settle = 0.6 * s_since + 0.4 * s_rng
    else:
        since = 0

    parts = {
        'rise': _score_rise(rise),
        'drop': _score_drop(drop),
        'vol_dry': _score_vol(volr),
        'body': _score_body(body),
        'decay': _clamp(decay),
        'settle': _clamp(settle),
    }
    score = sum(parts[k] * WEIGHTS[k] for k in WEIGHTS)
    detail = {k: round(parts[k] * WEIGHTS[k], 1) for k in WEIGHTS}

    # 反転確認（必須条件にはせず、起きていればフラグを立てるだけ）
    flags = []
    if m >= 2 and w[-2]['h'] and last['h'] > w[-2]['h']:
        flags.append('前日高値突破')
    if m >= 6 and last['c'] > max(b['h'] for b in w[-6:-1]):
        flags.append('5日高値突破')
    if m >= 6:
        base = [b['v'] for b in w[-6:-1] if b['v']]
        if base and last['v'] and last['c'] > last['o'] and last['v'] > 1.3 * (sum(base) / len(base)):
            flags.append('出来高増の陽線')

    # 直近3日の騰落率
    r3 = None
    if m >= 4 and w[-4]['c']:
        r3 = (last['c'] / w[-4]['c'] - 1) * 100

    return {
        'score': round(score, 1),
        'detail': detail,
        'rise': round(rise, 2),
        'drop': round(drop, 2),
        'vol_ratio': round(volr, 3) if volr is not None else None,
        'body': round(body, 3) if body is not None else None,
        'r3': round(r3, 2) if r3 is not None else None,
        'hi20': round(high, 1),
        'lo20': round(low, 1),
        'close': round(last['c'], 1),
        'date': last['d'],
        'days_since_low': since,
        'pullback_days': len(pb),
        'flags': flags,
        'turnover': round((last['c'] * (last['v'] or 0)) / 1e6, 1),   # 百万円
    }


def ranking(limit=50, min_turnover_m=50.0, only_reversed=False):
    """全銘柄を採点して上位を返す。
    min_turnover_m: 直近平均の売買代金（百万円）の下限。薄商いを除く。
    """
    conn = db.get_conn()
    ph = ','.join('?' * len(db.STOCK_MARKETS))
    rows = conn.execute(f"""
        WITH r AS (
          SELECT p.code, p.date, p.open, p.high, p.low, p.close, p.adj_close, p.volume,
                 ROW_NUMBER() OVER (PARTITION BY p.code ORDER BY p.date DESC) rn
          FROM daily_prices p
          JOIN stocks s ON s.code = p.code
          WHERE s.market IN ({ph})
        )
        SELECT * FROM r WHERE rn <= ? ORDER BY code, date
    """, list(db.STOCK_MARKETS) + [WINDOW + 6]).fetchall()
    names = {r['code']: r['name'] for r in conn.execute('SELECT code,name FROM stocks')}
    conn.close()

    by = {}
    for r in rows:
        by.setdefault(r['code'], []).append(r)

    out = []
    for code, rs in by.items():
        bars = []
        tos = []
        for r in rs:
            o, h, l, c, v = r['open'], r['high'], r['low'], r['close'], r['volume']
            if None in (o, h, l, c):
                continue
            # 分割・配当で過去が飛ばないよう、調整後終値との比で全値段を補正する
            f = (r['adj_close'] / c) if (r['adj_close'] and c) else 1.0
            bars.append({'d': r['date'], 'o': o * f, 'h': h * f,
                         'l': l * f, 'c': c * f, 'v': v})
            if c and v:
                tos.append(c * v)
        if not bars or not tos:
            continue
        if (sum(tos) / len(tos)) / 1e6 < min_turnover_m:
            continue                                       # 薄商いは除外
        res = compute(bars)
        if not res:
            continue
        if only_reversed and not res['flags']:
            continue
        res['code'] = code
        res['name'] = names.get(code, '')
        out.append(res)

    out.sort(key=lambda x: x['score'], reverse=True)
    return out[:limit]


def describe():
    """スコアの決め方（画面の説明用）"""
    return {
        'title': '📈 上昇後・調整終了候補',
        'text': [
            'このランキングは「一度しっかり上昇した銘柄が、その後の調整を終えつつある」'
            '段階を探すものです。',
            '狙っているのは 上昇 → 利確・調整 → 売り物が減る → 値動きが収束 という流れの'
            '最後の部分で、まだ反転していない段階です。',
            'そのため高値突破や陽線は必須条件にしていません。'
            '起きている場合だけ「反転確認済み」として印を付けます（見つけるのが遅れないように）。',
            '空売り残高・空売り増減はこのスコアに使っていません。踏み上げ系のランキングとは'
            '完全に別物です。',
        ],
        'rows': [{'key': k, 'name': LABEL[k][0], 'weight': WEIGHTS[k],
                  'desc': LABEL[k][1]} for k in WEIGHTS],
        'notes': [
            f'直近{WINDOW}営業日の中で「安値→その後の高値」を探し、上昇率が{MIN_RISE}%未満の'
            '銘柄は対象外にしています。',
            '高値が直近すぎて調整が始まっていない銘柄も対象外です。',
            '株価は分割・配当の影響を除いた調整後の値で計算しています。',
        ],
    }
