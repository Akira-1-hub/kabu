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
    except Exception as e:
        scan_state['status'] = f'エラー: {e}'
    finally:
        scan_state['running'] = False


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


@app.route('/short')
def short_page():
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
    rank = db.short_change_ranking(period, limit=50, **kw)
    new_short = db.short_new_entries(period, limit=50, **kw)
    squeeze = db.squeeze_ranking(period, limit=50, **kw)
    cover = db.cover_rally_ranking(period, limit=50, **kw)
    top_ratio = db.short_top_ratio(50)
    _add_cap_short(squeeze['rows'], cover['rows'], rank['increase'],
                   rank['decrease'], new_short['entries'], top_ratio)
    return render_template('short.html',
                           period=period,
                           custom_from=custom_from,
                           custom_to=custom_to,
                           rank=rank,
                           new_short=new_short,
                           squeeze=squeeze,
                           cover=cover,
                           top_ratio=top_ratio,
                           info=db.short_data_range())


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


@app.route('/heatmap')
def heatmap_page():
    return render_template('heatmap.html', rows=db.heatmap_rows())


@app.route('/rankings')
def rankings():
    prices = db.get_latest_prices()
    by_surge = sorted([p for p in prices if p.get('volume_ratio')],
                      key=lambda x: x['volume_ratio'], reverse=True)[:50]
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
                           hit_ranking=hits)


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
