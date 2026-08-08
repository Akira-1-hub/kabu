"""
投資データベース＆ダッシュボード - Flask アプリ
"""
from flask import Flask, render_template, jsonify, request, Response
import threading
import json
import csv
import io
import os
import sys
from datetime import datetime, timedelta

import db
import fetch
import themes as themes_mod

app = Flask(__name__)

# 空売り更新の状態
short_update_state = {'running': False, 'log': [], 'added': 0}

# ---- スキャン状態（グローバル） ----
scan_state = {
    'running': False,
    'status': '待機中',
    'progress': 0,
    'total': 0,
    'hits': 0,
    'results': [],
    'last_scan': None,
}
_stop_flag = threading.Event()
_scan_thread = None


# ============================================================
# スキャン実行
# ============================================================
def _do_scan(scope, min_pct, surge, mode, workers):
    global scan_state
    scan_state.update({'running': True, 'status': 'スキャン開始...', 'progress': 0, 'hits': 0, 'results': []})

    def cb(done, total, hits):
        scan_state.update({'progress': done, 'total': total, 'hits': hits,
                           'status': f'スキャン中... {done}/{total}'})

    try:
        # result_sink にライブで溜める（収集中もフロントに反映）
        results = fetch.scan(
            scope=scope, min_pct=min_pct, surge=surge, mode=mode,
            workers=workers, progress_cb=cb, stop_flag=_stop_flag,
            result_sink=scan_state['results'],
        )
        scan_state['last_scan'] = datetime.now().strftime('%m/%d %H:%M')
        scan_state['status'] = f'完了 {len(results)}銘柄取得 ({datetime.now().strftime("%H:%M:%S")})'
        # 上昇率上位の営業利益率を裏で取得しておく（次に上昇率ページを見たとき表示用）
        try:
            top = [g['code'] for g in db.gainers_ranking(limit=60)['rows']]
            threading.Thread(target=lambda: fetch.ensure_fundamentals(top, max_workers=12),
                             daemon=True).start()
        except Exception:
            pass
        # テスト用システムの日次更新（本番の後に実行。本番へは何も反映しない）
        threading.Thread(target=_run_test_daily, daemon=True).start()
    except Exception as e:
        scan_state['status'] = f'エラー: {e}'
    finally:
        scan_state['running'] = False


def _run_test_daily(asof=None, log=None):
    """テスト用システムの日次更新（仕様書11の4〜7）。
      4. 現在のテスト用モデルで特徴量とランキングを作り、その日の内容を固定保存
      5. 評価期間を迎えた過去の予測に、実際の株価結果を紐づける
    学習（8〜11）はここでは自動実行せず、画面の「学習を実行」から明示的に行う。
    データ欠損時は無理に進めない。
    """
    import test_features as tf
    import test_model as tm
    import test_outcome
    say = log or (lambda m: test_state['log'].append(str(m)))
    test_state.update({'running': True, 'status': 'テスト用を更新中...', 'log': []})
    try:
        asof = asof or db.latest_price_date()
        if not asof:
            test_state['status'] = '株価データがないため中止'
            return
        rows = tf.build(asof)
        if not rows:
            test_state['status'] = f'{asof}: 対象銘柄なし（空売りデータ待ち）'
            return
        tf.save(asof, rows)
        model = tm.current_model()
        tm.predict(asof, rows, model, limit=200)
        say(f'{asof}: {len(rows):,}銘柄の特徴量とランキングを保存（{model["version"]}）')
        n = test_outcome.fill(log=say)
        test_state['status'] = f'{asof} 更新完了（予測{len(rows):,}件／結果{n:,}件）'
    except Exception as e:
        test_state['status'] = f'エラー: {e}'
    finally:
        test_state['running'] = False
        test_state['finished'] = datetime.now().strftime('%m/%d %H:%M')


# ============================================================
# ページ
# ============================================================
@app.route('/')
def dashboard():
    today = datetime.now().strftime('%Y-%m-%d')
    return render_template('dashboard.html',
                           today_hits=db.get_hits_by_date(today),
                           watchlist=db.get_watchlist(),
                           recent_runs=db.get_recent_runs(5),
                           recent_tags=db.recent_flow_tags(12),
                           flow_label=db.FLOW_LABEL,
                           hit_ranking=db.get_hit_count_ranking(30)[:10])


@app.route('/stocks')
def stocks():
    name_map = {s['code']: s['name'] for s in db.list_tradable_codes()}
    return render_template('stocks.html', prices=db.get_latest_prices(), name_map=name_map)


@app.route('/stock/<code>')
def stock_detail(code):
    s = db.get_stock(code) or {'code': code, 'name': '', 'market': '', 'sector': ''}

    # ファンダ: クリック時取得＋当日キャッシュ（失敗しても旧データ表示）
    fund = db.get_fundamentals(code)
    today = datetime.now().strftime('%Y-%m-%d')
    if fund is None or fund.get('updated') != today:
        try:
            f = fetch.fetch_fundamentals(code)
            if f:
                db.save_fundamentals(f)
                fund = db.get_fundamentals(code)
        except Exception:
            pass

    themes = themes_mod.detect_themes(
        s.get('name'), s.get('sector'), (fund or {}).get('description'))
    size = themes_mod.size_tag((fund or {}).get('market_cap_oku'))

    # 建単価は「直近3ヶ月」を既定に（踏み上げの攻防ライン）。3Mに売買が無ければエピソード
    sm = db.short_max_date()
    cost_default = '66'
    cost_basis = None
    if sm:
        cb_from = (datetime.strptime(sm, '%Y-%m-%d') - timedelta(days=db.RECENT_DAYS)).strftime('%Y-%m-%d')
        cost_basis = db.short_cost_basis(code, from_date=cb_from)
    if not cost_basis or not cost_basis.get('rows'):
        cost_basis = db.short_cost_basis(code)   # 3Mに売買なし→エピソード
        cost_default = 'ep'

    return render_template('detail.html',
                           stock=s,
                           fund=fund,
                           themes=themes,
                           size_tag=size,
                           watched=db.is_watched(code),
                           history=db.get_price_history(code, 120),
                           hits=db.get_stock_hit_history(code),
                           shorts_latest=db.get_short_latest_by_institution(code),
                           short_report=db.short_report_history(code),
                           short_daily=db.get_short_daily_total(code),
                           cost_basis=cost_basis,
                           cost_default=cost_default,
                           flow_tags=db.get_flow_tags(code),
                           flow_label=db.FLOW_LABEL,
                           memos=db.get_memos(code))


@app.route('/api/flow_tag/<code>', methods=['POST'])
def api_flow_tag(code):
    d = request.json or {}
    action = d.get('action', 'save')
    if action == 'delete':
        db.delete_flow_tag(code, d.get('date'))
    else:
        db.save_flow_tag(code, d.get('tag', ''), d.get('memo', ''))
    return jsonify({'ok': True, 'tags': db.get_flow_tags(code)})


@app.route('/api/flow_marks/<code>')
def api_flow_marks(code):
    return jsonify({t['date']: t['tag'] for t in db.get_flow_tags(code)})


@app.route('/api/cost_band/<code>')
def api_cost_band(code):
    cb = db.short_cost_basis(code)
    return jsonify(cb.get('agg'))


@app.route('/api/cost_basis/<code>')
def api_cost_basis(code):
    frm = request.args.get('from') or None
    to = request.args.get('to') or None
    return jsonify(db.short_cost_basis(code, frm, to))


@app.route('/api/short_daily/<code>')
def api_short_daily(code):
    return jsonify(db.get_short_daily_total(code))


def _start_short_update():
    """空売り更新をバックグラウンドで開始（多重起動防止）"""
    if short_update_state['running']:
        return False

    def run():
        import fetch_jpx
        short_update_state.update({'running': True, 'log': [], 'added': 0})
        try:
            short_update_state['added'] = fetch_jpx.import_jpx(
                log=lambda m: short_update_state['log'].append(str(m)))
        except Exception as e:
            short_update_state['log'].append(f'エラー: {e}')
        finally:
            short_update_state['running'] = False

    threading.Thread(target=run, daemon=True).start()
    return True


@app.route('/api/short/update', methods=['POST'])
def api_short_update():
    """JPXから最新の空売りデータを取得してDBに追加"""
    if not _start_short_update():
        return jsonify({'ok': False, 'msg': '更新中です'})
    return jsonify({'ok': True})


@app.route('/api/short/update_status')
def api_short_update_status():
    info = db.short_data_range()
    gaps = db.short_gaps()
    return jsonify({
        'running': short_update_state['running'],
        'log': short_update_state['log'][-20:],
        'added': short_update_state['added'],
        'total': info.get('n', 0),
        'max_date': info.get('max_d'),
        'codes': info.get('codes', 0),
        'days': info.get('days', 0),
        'gaps': gaps[-10:],
        'gap_count': len(gaps),
    })


# ============================================================
# 企業情報の一括取得（全銘柄）
# ============================================================
fund_state = {'running': False, 'done': 0, 'total': 0, 'ok': 0,
              'status': '', 'finished': None}
fund_stop = threading.Event()


def _fund_run(force):
    fund_stop.clear()
    fund_state.update({'running': True, 'done': 0, 'total': 0, 'ok': 0,
                       'status': '対象を集計中...', 'finished': None})
    try:
        codes = [s['code'] for s in db.list_tradable_codes()]

        def cb(done, total, ok):
            fund_state.update({'done': done, 'total': total, 'ok': ok,
                               'status': f'取得中 {done}/{total}（成功{ok}）'})

        r = fetch.fetch_all_fundamentals(codes, max_workers=12, force=force,
                                         progress_cb=cb, stop_flag=fund_stop)
        if fund_stop.is_set():
            fund_state['status'] = f'中断しました（{r["ok"]}件取得）'
        elif r['total'] == 0:
            fund_state['status'] = '全銘柄すでに取得済みです（本日分）'
        else:
            fund_state['status'] = f'完了 {r["ok"]}/{r["total"]}件を取得しました'
    except Exception as e:
        fund_state['status'] = f'エラー: {e}'
    finally:
        fund_state['running'] = False
        fund_state['finished'] = datetime.now().strftime('%m/%d %H:%M')


@app.route('/api/fundamentals/all', methods=['POST'])
def api_fundamentals_all():
    if fund_state['running']:
        return jsonify({'ok': False, 'msg': '取得中です'})
    force = bool((request.json or {}).get('force'))
    threading.Thread(target=_fund_run, args=(force,), daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/fundamentals/stop', methods=['POST'])
def api_fundamentals_stop():
    fund_stop.set()
    fund_state['status'] = '中断中...'
    return jsonify({'ok': True})


@app.route('/api/fundamentals/status')
def api_fundamentals_status():
    have = db.count_fundamentals()
    return jsonify({
        'running': fund_state['running'],
        'done': fund_state['done'], 'total': fund_state['total'],
        'ok': fund_state['ok'], 'status': fund_state['status'],
        'finished': fund_state['finished'],
        'have': have, 'stocks': db.count_tradable(),
    })


# ============================================================
# 公開サイト更新（site/生成 → gh-pages へ force push）
# ============================================================
publish_state = {'running': False, 'log': [], 'ok': None, 'finished': None}
GH_REMOTE = 'https://github.com/Akira-1-hub/kabu.git'


def _publish_run():
    import subprocess
    import shutil
    base = os.path.dirname(os.path.abspath(__file__))
    site = os.path.join(base, 'site')

    def log(m):
        publish_state['log'].append(str(m))

    def run(args, cwd, label):
        """失敗したら例外。出力の末尾だけログに残す"""
        p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        if p.returncode != 0:
            tail = ((p.stderr or '') + (p.stdout or '')).strip().splitlines()
            raise RuntimeError(f'{label} 失敗: ' + (tail[-1] if tail else f'code {p.returncode}'))
        return p

    publish_state.update({'running': True, 'log': [], 'ok': None, 'finished': None})
    try:
        log('公開サイトを生成中...(1分ほど)')
        run([sys.executable, 'export_static.py'], base, 'サイト生成')
        log('生成完了。GitHubへ送信中...')

        gitdir = os.path.join(site, '.git')
        if os.path.isdir(gitdir):
            shutil.rmtree(gitdir, ignore_errors=True)
        run(['git', 'init', '-q', '-b', 'gh-pages'], site, 'git init')
        run(['git', 'config', 'user.name', 'akino'], site, 'git config')
        run(['git', 'config', 'user.email', 'akino@users.noreply.github.com'], site, 'git config')
        run(['git', 'add', '-A'], site, 'git add')
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        run(['git', 'commit', '-q', '-m', f'publish {stamp}'], site, 'git commit')
        run(['git', 'push', '-f', GH_REMOTE, 'gh-pages'], site, 'git push')
        shutil.rmtree(gitdir, ignore_errors=True)

        publish_state['ok'] = True
        log('完了！ 1〜2分でサイトに反映されます')
    except Exception as e:
        publish_state['ok'] = False
        log(f'エラー: {e}')
    finally:
        publish_state['running'] = False
        publish_state['finished'] = datetime.now().strftime('%m/%d %H:%M')


@app.route('/api/publish', methods=['POST'])
def api_publish():
    if publish_state['running']:
        return jsonify({'ok': False, 'msg': '公開更新中です'})
    threading.Thread(target=_publish_run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/publish/status')
def api_publish_status():
    return jsonify({
        'running': publish_state['running'],
        'log': publish_state['log'][-8:],
        'ok': publish_state['ok'],
        'finished': publish_state['finished'],
        'url': 'https://akira-1-hub.github.io/kabu/',
    })


@app.route('/watchlist')
def watchlist_page():
    wl = db.get_watchlist()
    latest = {p['code']: p for p in db.get_latest_prices([w['code'] for w in wl])}
    for w in wl:
        w['price'] = latest.get(w['code'])
    return render_template('watchlist.html', watchlist=wl)


margin_state = {'running': False}


@app.route('/gainers')
def gainers_page():
    falling = request.args.get('dir') == 'down'
    g = db.gainers_ranking(limit=100, falling=falling)
    return render_template('gainers.html', g=g, falling=falling)


@app.route('/api/gainers/margins', methods=['POST', 'GET'])
def api_gainers_margins():
    if request.method == 'GET':
        return jsonify({'running': margin_state['running']})
    if margin_state['running']:
        return jsonify({'ok': True})

    def run():
        margin_state['running'] = True
        try:
            top = [g['code'] for g in db.gainers_ranking(limit=80)['rows']]
            fetch.ensure_fundamentals(top, max_workers=12)
        finally:
            margin_state['running'] = False
    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


def _short_common(base, weights=None):
    """空売りランキングの中身を作る。本番と調整用で同じ処理を使う。
    weights を渡すとその重みで採点する（本番は None ＝ 固定値）。
    """
    period = request.args.get('period', 'weekly')   # 既定は週間（1週間前比）
    custom_from = request.args.get('from') or None
    custom_to = request.args.get('to') or None
    if period == 'custom' and not (custom_from or custom_to):
        period = 'weekly'
    # 'daily'/'weekly'/'thisweek'/'custom' のほか '14d' のような日数指定を許可
    if period not in ('daily', 'weekly', 'thisweek', 'custom') and not (
            period.endswith('d') and period[:-1].isdigit() and 0 < int(period[:-1]) <= 730):
        period = 'weekly'
    kw = {'custom_from': custom_from, 'custom_to': custom_to}
    sq_w = (weights or {}).get('squeeze')
    cv_w = (weights or {}).get('cover')
    rank = db.short_change_ranking(period, limit=50, **kw)
    new_short = db.short_new_entries(period, limit=50, **kw)
    squeeze = db.squeeze_ranking(period, limit=50, weights=sq_w, **kw)
    cover = db.cover_rally_ranking(period, limit=50, weights=cv_w, **kw)
    top_ratio = db.short_top_ratio(50)
    _add_cap_short(squeeze['rows'], cover['rows'], rank['increase'],
                   rank['decrease'], new_short['entries'], top_ratio)
    return dict(base=base, period=period, custom_from=custom_from,
                custom_to=custom_to, rank=rank, new_short=new_short,
                squeeze=squeeze, cover=cover, top_ratio=top_ratio,
                info=db.short_data_range())


@app.route('/short')
def short_page():
    return render_template('short.html', variant='main',
                           **_short_common('/short'))


@app.route('/short-b')
def short_b_page():
    """空売りランキング（調整用）。重みを変えて試す場所。
    本番の /short には影響しない。過去の重みはいつでも戻せる。
    """
    w = db.alt_current()
    return render_template('short.html', variant='b', alt=w,
                           alt_list=db.alt_list(),
                           **_short_common('/short-b', w))


@app.route('/pullback')
def pullback_page():
    """上昇後・調整終了候補ランキング（空売り系とは独立）"""
    import pullback as pb
    try:
        mt = float(request.args.get('mt', 50))
    except (TypeError, ValueError):
        mt = 50.0
    only_rev = bool(request.args.get('rev'))
    rows = pb.ranking(limit=60, min_turnover_m=mt, only_reversed=only_rev)
    _add_cap_short(rows)
    return render_template('pullback.html', rows=rows, mt=mt, only_rev=only_rev,
                           info=pb.describe(), weights=pb.WEIGHTS,
                           weights_label={k: v[0] for k, v in pb.LABEL.items()})


@app.route('/api/pullback/describe')
def api_pullback_describe():
    import pullback as pb
    return jsonify(pb.describe())


world_state = {'running': False, 'status': '', 'finished': None}


def _world_rows():
    """指数・米国株の一覧（表示名などのメタを付けて返す）"""
    import fetch_world
    latest = db.world_latest()
    out = []
    for sym, name, kind, unit in fetch_world.SYMBOLS:
        d = latest.get(sym)
        if not d:
            continue
        out.append({
            'symbol': sym, 'name': name, 'kind': kind, 'unit': unit,
            'date': d['date'], 'close': d['close'],
            'change': d['change'], 'pct': d['pct'], 'prev_close': d['prev_close'],
        })
    return out


@app.route('/world')
def world_page():
    rows = _world_rows()
    return render_template('world.html',
                           idx=[r for r in rows if r['kind'] == 'index'],
                           us=sorted([r for r in rows if r['kind'] == 'us'],
                                     key=lambda x: (x['pct'] is None, -(x['pct'] or 0))),
                           info=db.world_range())


@app.route('/api/world/update', methods=['POST'])
def api_world_update():
    if world_state['running']:
        return jsonify({'ok': False, 'msg': '取得中です'})

    def run():
        import fetch_world
        world_state.update({'running': True, 'status': '取得中...'})
        try:
            n = fetch_world.fetch_world(period='1mo', log=lambda m: None)
            world_state['status'] = f'{n}件を更新しました'
        except Exception as e:
            world_state['status'] = f'エラー: {e}'
        finally:
            world_state['running'] = False
            world_state['finished'] = datetime.now().strftime('%m/%d %H:%M')

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/world/bars/<path:symbol>')
def api_world_bars(symbol):
    return jsonify(db.world_bars(symbol, days=500))


@app.route('/api/world/status')
def api_world_status():
    return jsonify({'running': world_state['running'], 'status': world_state['status'],
                    'finished': world_state['finished'], **db.world_range()})


# ============================================================
# 空売り（テスト用）  ※本番ランキングとは完全に分離。学習結果は本番に反映しない
# ============================================================
test_state = {'running': False, 'status': '', 'log': [], 'finished': None}


@app.route('/short-test')
def short_test_page():
    import test_model as tm
    import json as _json
    tab = request.args.get('tab', 'latest')
    date = request.args.get('date')
    model = tm.current_model()

    conn = db.get_conn()
    days = [r['date'] for r in conn.execute(
        'SELECT DISTINCT date FROM test_predictions ORDER BY date DESC LIMIT 400')]
    target = date if (date and date in days) else (days[0] if days else None)

    rows = []
    if target:
        rows = [dict(r) for r in conn.execute("""
            SELECT p.rank,p.code,p.score,p.contrib,p.confidence,p.version,
                   f.close,f.sector,f.market_cap,f.short_ratio,f.feats,f.data_date,
                   s.name,
                   o.r5,o.mx5,o.r10,o.mx10,o.ex5
            FROM test_predictions p
            LEFT JOIN test_features f ON f.date=p.date AND f.code=p.code
            LEFT JOIN stocks s ON s.code=p.code
            LEFT JOIN test_outcomes o ON o.date=p.date AND o.code=p.code
            WHERE p.date=? ORDER BY p.rank LIMIT 50
        """, (target,))]
        for r in rows:
            r['contrib'] = _json.loads(r['contrib'] or '{}')
            r['feats'] = _json.loads(r['feats'] or '{}')
    stats = dict(conn.execute("""
        SELECT (SELECT COUNT(*) FROM test_features) n_feat,
               (SELECT COUNT(DISTINCT date) FROM test_features) n_days,
               (SELECT COUNT(*) FROM test_outcomes) n_out,
               (SELECT COUNT(*) FROM test_predictions) n_pred
    """).fetchone())
    conn.close()

    return render_template('short_test.html', tab=tab, model=model, days=days,
                           target=target, rows=rows, stats=stats,
                           models=tm.list_models())


@app.route('/api/test/summary')
def api_test_summary():
    """テスト用モデルの成績と、本番ランキングとの比較"""
    import test_eval
    import test_model as tm
    m = tm.current_model()
    rows = test_eval.load_joined(m['version'])
    return jsonify({'version': m['version'],
                    'metrics': test_eval.evaluate(rows, main='mx5')})


@app.route('/api/short/weights')
def api_short_weights():
    """スコアの決め方（ツール側の説明表示用）。?alt=1 で調整用の重み"""
    if request.args.get('alt'):
        w = db.alt_current()
        d = db.describe_weights(w['squeeze'], w['cover'])
        d['version'] = w['version']
        d['note'] = w['note']
        return jsonify(d)
    return jsonify(db.describe_weights())


@app.route('/api/short-b/weights', methods=['POST'])
def api_short_b_save():
    """調整用の重みを新しいバージョンとして保存する（既存は消さない）"""
    d = request.json or {}
    cur = db.alt_current()
    sq = dict(cur['squeeze'])
    cv = dict(cur['cover'])
    for k, v in (d.get('squeeze') or {}).items():
        if k in sq:
            try:
                sq[k] = max(0.0, min(50.0, float(v)))
            except (TypeError, ValueError):
                pass
    for k, v in (d.get('cover') or {}).items():
        if k in cv:
            try:
                cv[k] = max(0.0, min(50.0, float(v)))
            except (TypeError, ValueError):
                pass
    if sq == cur['squeeze'] and cv == cur['cover']:
        return jsonify({'ok': False, 'msg': '変更がありません'})
    new = db.alt_save(sq, cv, note=(d.get('note') or '').strip())
    return jsonify({'ok': True, 'version': new['version']})


@app.route('/api/short-b/use', methods=['POST'])
def api_short_b_use():
    """過去のバージョンに戻す"""
    v = (request.json or {}).get('version')
    if not v or not db.alt_use(v):
        return jsonify({'ok': False, 'msg': 'そのバージョンが見つかりません'})
    return jsonify({'ok': True, 'version': v})


@app.route('/api/test/model')
def api_test_model():
    """モデルの中身を日本語で説明したもの（バージョンをクリックしたとき用）"""
    import test_model as tm
    return jsonify(tm.describe(request.args.get('version')) or {})


@app.route('/api/test/learn_ready')
def api_test_learn_ready():
    """今『学習を実行』を押す価値があるかの目安"""
    import test_learn
    import test_model as tm
    m = tm.current_model()
    conn = db.get_conn()
    n_out = conn.execute('SELECT COUNT(*) c FROM test_outcomes').fetchone()['c']
    n_days = conn.execute(
        'SELECT COUNT(DISTINCT date) c FROM test_outcomes').fetchone()['c']
    # 前回の学習以降に増えた結果件数
    since = m.get('created_at') or ''
    n_new = conn.execute(
        'SELECT COUNT(*) c FROM test_outcomes WHERE filled_at > ?', (since,)).fetchone()['c']
    conn.close()
    enough = n_days >= test_learn.MIN_DAYS and n_out >= test_learn.MIN_SAMPLES
    if not enough:
        msg = (f'学習にはあと {max(0, test_learn.MIN_DAYS - n_days)}日 / '
               f'{max(0, test_learn.MIN_SAMPLES - n_out):,}件 必要です')
    elif n_new < 2000:
        msg = (f'前回のモデル作成以降に増えた結果は {n_new:,}件です。'
               'まだ材料が少ないので、押しても結果は変わりにくいです（月1回程度が目安）')
    else:
        msg = f'前回以降 {n_new:,}件の結果が増えています。学習を試す価値があります'
    return jsonify({'enough': enough, 'n_out': n_out, 'n_days': n_days,
                    'n_new': n_new, 'msg': msg,
                    'min_days': test_learn.MIN_DAYS,
                    'min_samples': test_learn.MIN_SAMPLES})


@app.route('/api/test/update', methods=['POST'])
def api_test_update():
    """テスト用を今すぐ更新（通常はスキャン完了時に自動実行される）"""
    if test_state['running']:
        return jsonify({'ok': False, 'msg': '実行中です'})
    threading.Thread(target=_run_test_daily, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/test/learn', methods=['POST'])
def api_test_learn():
    if test_state['running']:
        return jsonify({'ok': False, 'msg': '実行中です'})

    def run():
        import test_learn
        test_state.update({'running': True, 'log': [], 'status': '学習中...'})
        try:
            r = test_learn.run(log=lambda m: test_state['log'].append(str(m)))
            test_state['status'] = (f'新モデル {r["version"]} を採用' if r
                                    else '条件を満たす候補なし（現行を維持）')
        except Exception as e:
            test_state['status'] = f'エラー: {e}'
        finally:
            test_state['running'] = False
            test_state['finished'] = datetime.now().strftime('%m/%d %H:%M')

    threading.Thread(target=run, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/test/status')
def api_test_status():
    return jsonify(test_state)


@app.route('/heatmap')
def heatmap_page():
    return render_template('heatmap.html', rows=db.heatmap_rows())


@app.route('/rankings')
def rankings():
    prices = db.get_latest_prices()

    # 出来高急増の絞り込み：上昇率の下限/上限（急騰済みを外す・下落中の急増を拾う等）
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    smin, smax = _f(request.args.get('smin')), _f(request.args.get('smax'))
    surge_src = [p for p in prices if p.get('volume_ratio')]
    if smin is not None or smax is not None:
        surge_src = [p for p in surge_src if p.get('change_pct') is not None
                     and (smin is None or p['change_pct'] >= smin)
                     and (smax is None or p['change_pct'] <= smax)]
    by_surge = sorted(surge_src, key=lambda x: x['volume_ratio'], reverse=True)[:50]
    by_rise = sorted([p for p in prices if p.get('change_pct') is not None],
                     key=lambda x: x['change_pct'], reverse=True)[:50]
    by_fall = sorted([p for p in prices if p.get('change_pct') is not None],
                     key=lambda x: x['change_pct'])[:50]
    name_map = {s['code']: s['name'] for s in db.list_tradable_codes()}
    hits = db.get_hit_count_ranking(30)
    for lst in (by_surge, by_rise, by_fall):
        for p in lst:
            p['name'] = name_map.get(p['code'], '')
    _add_cap_short(by_surge, by_rise, by_fall, hits)
    return render_template('rankings.html',
                           by_surge=by_surge, by_rise=by_rise, by_fall=by_fall,
                           hit_ranking=hits,
                           smin=request.args.get('smin', ''),
                           smax=request.args.get('smax', ''))


def _add_cap_short(*row_lists):
    """行リストに時価総額(億円)と空売り比率(%)を付与（1回のDB読み込みで全リスト分）"""
    funds = db.get_all_fundamentals()
    latest = db.short_max_date()
    totals = db.short_totals_asof(latest) if latest else {}
    for rows in row_lists:
        for r in rows:
            code = r.get('code')
            f = funds.get(code)
            r['market_cap_oku'] = f.get('market_cap_oku') if f else None
            t = totals.get(code)
            r['short_ratio'] = round(t['total_ratio'], 2) if t and t.get('total_ratio') else None


# ============================================================
# API
# ============================================================
@app.route('/api/scan/start', methods=['POST'])
def api_scan_start():
    global _scan_thread
    if scan_state['running']:
        return jsonify({'ok': False, 'msg': 'スキャン実行中です'})
    d = request.json or {}
    _stop_flag.clear()
    _scan_thread = threading.Thread(target=_do_scan, kwargs={
        'scope': d.get('scope', 'all'),
        'min_pct': float(d.get('min_pct', 3.0)),
        'surge': float(d.get('surge', 2.0)),
        'mode': d.get('mode', 'both'),
        'workers': int(d.get('workers', 20)),
    }, daemon=True)
    _scan_thread.start()
    # 空売り更新＆TDnet開示も同時に走らせる（軽いので並行でOK）
    _start_short_update()
    threading.Thread(target=lambda: __import__('fetch_tdnet').import_tdnet(days=2, log=lambda m: None),
                     daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/scan/stop', methods=['POST'])
def api_scan_stop():
    _stop_flag.set()
    scan_state['status'] = '停止中...'
    return jsonify({'ok': True})


@app.route('/api/scan/status')
def api_scan_status():
    return jsonify({
        'running': scan_state['running'],
        'status': scan_state['status'],
        'progress': scan_state['progress'],
        'total': scan_state['total'],
        'hits': scan_state['hits'],
        'last_scan': scan_state['last_scan'],
    })


@app.route('/api/scan/results')
def api_scan_results():
    # 収集中も読めるようスナップショットを返す（イテレーション衝突回避）
    return jsonify(list(scan_state['results']))


@app.route('/api/watch/<code>', methods=['POST'])
def api_watch(code):
    if db.is_watched(code):
        db.remove_watch(code)
        return jsonify({'watched': False})
    db.add_watch(code)
    return jsonify({'watched': True})


@app.route('/api/watched')
def api_watched():
    return jsonify([w['code'] for w in db.get_watchlist()])


@app.route('/api/memo/<code>', methods=['POST'])
def api_memo(code):
    text = (request.json or {}).get('text', '').strip()
    if text:
        db.add_memo(code, text)
    return jsonify({'ok': True, 'memos': db.get_memos(code)})


@app.route('/api/price_history/<code>')
def api_price_history(code):
    h = db.get_price_history(code, 600)
    h.reverse()  # 古い順
    return jsonify(h)


# ============================================================
# CSV出力
# ============================================================
def _csv_response(rows, fields, filename):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction='ignore')
    w.writeheader()
    w.writerows(rows)
    return Response(
        '﻿' + buf.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@app.route('/export/scan')
def export_scan():
    return _csv_response(
        scan_state['results'],
        ['code', 'name', 'date', 'close', 'change', 'change_pct', 'volume', 'volume_ratio', 'cond'],
        f'scan_{datetime.now():%Y%m%d_%H%M}.csv'
    )


@app.route('/export/watchlist')
def export_watchlist():
    return _csv_response(db.get_watchlist(), ['code', 'name', 'market', 'sector', 'added_date', 'memo'],
                         f'watchlist_{datetime.now():%Y%m%d}.csv')


@app.route('/export/stock/<code>')
def export_stock(code):
    return _csv_response(
        db.get_price_history(code, 9999),
        ['date', 'open', 'high', 'low', 'close', 'change', 'change_pct', 'volume', 'avg_volume', 'volume_ratio'],
        f'{code}_history.csv'
    )


# ============================================================
# 起動
# ============================================================
def main():
    import socket, webbrowser, time
    db.init_db()
    if not db.list_tradable_codes():
        db.load_stock_master_from_csv()

    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = 'localhost'

    import sys
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    print('\n' + '=' * 52)
    print('  投資データベース＆ダッシュボード')
    print('=' * 52)
    print(f'  PC用:    http://localhost:5000')
    print(f'  スマホ用: http://{local_ip}:5000')
    print('=' * 52 + '\n')

    def open_browser():
        time.sleep(1.5)
        webbrowser.open('http://localhost:5000')
    threading.Thread(target=open_browser, daemon=True).start()

    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
