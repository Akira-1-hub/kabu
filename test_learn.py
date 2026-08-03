"""
空売り（テスト用）：重みの学習

やり方は「透明性の高い重み最適化」（仕様書13・14の第2段階）。
ブラックボックスではなく、各項目の効き方を数値で出してから重みを作る。

  1. 学習期間で「特徴量 と 将来リターン」の順位相関(IC)を出す
  2. ICから重み候補を作る（現行重みからの変化量は制限する）
  3. 学習に使っていない検証期間で候補を評価する
  4. 現行モデルに安定して勝った候補だけ採用する

時系列分割のみ。ランダム分割はしない（未来情報の混入防止）。
"""
import json
import sys
from collections import defaultdict

import db
import test_eval
import test_model as tm
from test_features import FEATURE_NAMES

# ---- 採用の門番（仕様書8：過学習を防ぐ）----
W_MIN, W_MAX = 0.0, 15.0     # 重みの上下限
MAX_STEP = 0.30              # 1回の学習で動かせる割合（±30%）
MIN_DAYS = 60                # 学習に必要な最低日数
MIN_SAMPLES = 500            # 学習に必要な最低予測件数
MIN_GAIN = 0.15              # 検証でこれ以上改善しないと採用しない(%)
MAX_CODE_SHARE = 35.0        # 上位が特定銘柄に偏りすぎたら不採用(%)
MAX_SECTOR_SHARE = 60.0      # 業種偏り上限(%)
MAIN = 'mx5'                 # 主評価指標（5営業日以内の最大上昇率）


def feature_ic(frm, to, target='mx5'):
    """学習期間で、各特徴量と将来リターンの順位相関を出す。
    「どの項目が効いているか」を示す説明用の数値でもある。
    """
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT f.date,f.code,f.feats,o.mx5,o.r5,o.r10,o.mx10,o.ex5
        FROM test_features f
        JOIN test_outcomes o ON f.date=o.date AND f.code=o.code
        WHERE f.date >= ? AND f.date <= ?
    """, (frm, to)).fetchall()
    conn.close()
    if not rows:
        return {}, 0

    by_date = defaultdict(list)
    for r in rows:
        y = r[target]
        if y is None:
            continue
        by_date[r['date']].append((json.loads(r['feats']), y))

    acc = defaultdict(list)
    for d, recs in by_date.items():
        if len(recs) < 10:
            continue
        for name in FEATURE_NAMES:
            pairs = [(f.get(name), y) for f, y in recs if f.get(name) is not None]
            if len(pairs) < 10:
                continue
            c = test_eval._spearman(pairs)
            if c is not None:
                acc[name].append(c)
    ic = {k: round(sum(v) / len(v), 4) for k, v in acc.items() if v}
    return ic, len(rows)


def candidates_from_ic(cur_w, ic):
    """ICから重み候補を作る。現行からの変化はMAX_STEPまでに抑える。"""
    out = []
    pos = {k: max(0.0, v) for k, v in ic.items()}
    tot = sum(pos.values())
    if tot <= 0:
        return out

    # 候補A: ICの比率に寄せる（ただし現行から±MAX_STEPまで）
    scale = sum(cur_w.get(k, 0) for k in FEATURE_NAMES) or 1.0
    target = {k: pos.get(k, 0.0) / tot * scale for k in FEATURE_NAMES}
    a = {}
    for k in FEATURE_NAMES:
        c = cur_w.get(k, 0.0)
        t = target.get(k, 0.0)
        lim = max(0.5, abs(c) * MAX_STEP)
        a[k] = round(min(W_MAX, max(W_MIN, c + max(-lim, min(lim, t - c)))), 3)
    out.append(('IC比率へ寄せる', a))

    # 候補B: ICが負の項目だけ弱める（安全側の小さな一歩）
    b = dict(cur_w)
    for k, v in ic.items():
        if v < 0:
            b[k] = round(max(W_MIN, cur_w.get(k, 0) * (1 - MAX_STEP)), 3)
    if b != cur_w:
        out.append(('効いていない項目を弱める', b))

    # 候補C: ICが高い上位3項目だけ強める
    top = sorted(ic.items(), key=lambda x: -x[1])[:3]
    c3 = dict(cur_w)
    for k, v in top:
        if v > 0:
            c3[k] = round(min(W_MAX, max(0.5, cur_w.get(k, 0)) * (1 + MAX_STEP)), 3)
    if c3 != cur_w:
        out.append(('効いている上位3項目を強める', c3))
    return out


def score_with(weights, frm, to, limit=200):
    """指定の重みで期間内を採点し直し、評価に必要な形で返す（保存はしない）"""
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT f.date,f.code,f.feats,f.sector,
               o.r1,o.r3,o.r5,o.r10,o.r20,o.mx5,o.mx10,o.mx20,
               o.mn5,o.mn10,o.mn20,o.hit5,o.hit10,o.ex5,o.ex10,o.ex20
        FROM test_features f
        JOIN test_outcomes o ON f.date=o.date AND f.code=o.code
        WHERE f.date >= ? AND f.date <= ?
    """, (frm, to)).fetchall()
    conn.close()

    by_date = defaultdict(list)
    for r in rows:
        d = dict(r)
        s, _ = tm.score_row(json.loads(d.pop('feats')), weights)
        d['score'] = s
        by_date[d['date']].append(d)

    out = []
    for d, rs in by_date.items():
        rs.sort(key=lambda x: -x['score'])
        for i, x in enumerate(rs[:limit], 1):
            x['rank'] = i
            out.append(x)
    return out


def walk_forward(train_ratio=0.6, valid_ratio=0.2):
    """時系列で 学習 / 検証 / 最終確認 に分ける"""
    conn = db.get_conn()
    days = [r['date'] for r in conn.execute(
        'SELECT DISTINCT date FROM test_outcomes ORDER BY date')]
    conn.close()
    n = len(days)
    if n < MIN_DAYS:
        return None
    a = int(n * train_ratio)
    b = int(n * (train_ratio + valid_ratio))
    return {'train': (days[0], days[a - 1]),
            'valid': (days[a], days[b - 1]),
            'holdout': (days[b], days[-1]),
            'n_days': n}


def run(log=print, dry_run=False):
    """学習を1回実行する。採用条件を満たした場合だけ新モデルを作る。"""
    split = walk_forward()
    if not split:
        log(f'データ不足：学習には最低{MIN_DAYS}日必要です')
        return None
    tr, va, ho = split['train'], split['valid'], split['holdout']
    log(f'学習 {tr[0]}〜{tr[1]} / 検証 {va[0]}〜{va[1]} / 最終確認 {ho[0]}〜{ho[1]}')

    ic, n_samples = feature_ic(tr[0], tr[1], target=MAIN)
    if n_samples < MIN_SAMPLES:
        log(f'サンプル不足：{n_samples}件（最低{MIN_SAMPLES}件）。重みは変更しません')
        return None
    log(f'学習サンプル {n_samples:,}件')
    log('各項目の効き方(IC・大きいほど将来の上昇と関係が強い):')
    for k, v in sorted(ic.items(), key=lambda x: -x[1]):
        log(f'    {k:14s} {v:+.4f}')

    cur = tm.current_model()
    cur_metrics = test_eval.evaluate(score_with(cur['weights'], va[0], va[1]), main=MAIN)
    base = cur_metrics.get('main_value')
    log(f'\n現行 {cur["version"]} の検証成績: 上位10の{MAIN} = {base}')

    best = None
    for name, w in candidates_from_ic(cur['weights'], ic):
        m = test_eval.evaluate(score_with(w, va[0], va[1]), main=MAIN)
        v = m.get('main_value')
        ok, why = _passes(m, base, v)
        log(f'  候補「{name}」: {MAIN}={v} → {"採用可" if ok else "不採用（" + why + "）"}')
        test_eval.save_evaluation(cur['version'], va[0], va[1], 'validate', m,
                                  adopted=0, note=f'候補: {name}')
        if ok and (best is None or v > best[2]):
            best = (name, w, v, m)

    if not best:
        log('\n条件を満たす候補がありません。現行モデルを維持します')
        return None

    name, w, v, m = best
    # 最終確認（学習にも検証にも使っていない期間）
    hm = test_eval.evaluate(score_with(w, ho[0], ho[1]), main=MAIN)
    hb = test_eval.evaluate(score_with(cur['weights'], ho[0], ho[1]), main=MAIN)
    log(f'\n最終確認: 候補={hm.get("main_value")} / 現行={hb.get("main_value")}')
    if (hm.get('main_value') or -99) < (hb.get('main_value') or -99):
        log('最終確認で現行を下回ったため不採用。現行モデルを維持します')
        test_eval.save_evaluation(cur['version'], ho[0], ho[1], 'holdout', hm,
                                  adopted=0, note=f'不採用: {name}（最終確認で劣後）')
        return None

    if dry_run:
        log('（dry-run のためモデルは更新しません）')
        return {'weights': w, 'metrics': m}

    nm = tm.create_model(w, source='auto',
                         reason=f'{name}／検証{MAIN} {base}→{v}（最終確認{hm.get("main_value")}）',
                         train_from=tr[0], train_to=tr[1], n_train=n_samples,
                         metrics={'valid': m, 'holdout': hm, 'ic': ic})
    test_eval.save_evaluation(nm['version'], ho[0], ho[1], 'holdout', hm,
                              adopted=1, note=f'採用: {name}')
    log(f'\n新モデル {nm["version"]} を採用しました')
    return nm


def _passes(m, base, v):
    """採用の門番。1つでも引っかかれば不採用。"""
    if v is None or base is None:
        return False, 'データ不足'
    if v <= base + MIN_GAIN:
        return False, f'改善が小さい({v}≦{base}+{MIN_GAIN})'
    if (m.get('n_pred') or 0) < MIN_SAMPLES:
        return False, 'サンプル不足'
    if (m.get('top_code_share') or 0) > MAX_CODE_SHARE:
        return False, f'特定銘柄に偏り({m.get("top_code_share")}%)'
    if (m.get('top_sector_share') or 0) > MAX_SECTOR_SHARE:
        return False, f'特定業種に偏り({m.get("top_sector_share")}%)'
    if (m.get('monthly_win') or 0) < 40:
        return False, f'月別の勝率が低い({m.get("monthly_win")}%)'
    return True, ''


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    run(dry_run='--dry-run' in sys.argv)
