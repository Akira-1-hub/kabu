"""
投資データベース＆ダッシュボード - Flask アプリ
"""
from flask import Flask, render_template, jsonify, request, Response
import threading
import json
import csv
import io
from datetime import datetime

import db
import fetch

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

    return render_template('detail.html',
                           stock=s,
                           fund=fund,
                           watched=db.is_watched(code),
                           history=db.get_price_history(code, 120),
                           hits=db.get_stock_hit_history(code),
                           shorts_latest=db.get_short_latest_by_institution(code),
                           short_daily=db.get_short_daily_total(code),
                           cost_basis=db.short_cost_basis(code),
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


@app.route('/watchlist')
def watchlist_page():
    wl = db.get_watchlist()
    latest = {p['code']: p for p in db.get_latest_prices([w['code'] for w in wl])}
    for w in wl:
        w['price'] = latest.get(w['code'])
    return render_template('watchlist.html', watchlist=wl)


@app.route('/gainers')
def gainers_page():
    falling = request.args.get('dir') == 'down'
    g = db.gainers_ranking(limit=100, falling=falling)
    return render_template('gainers.html', g=g, falling=falling)


@app.route('/short')
def short_page():
    period = request.args.get('period', 'daily')
    if period not in ('daily', 'weekly', 'thisweek'):
        period = 'daily'
    rank = db.short_change_ranking(period, limit=50)
    new_short = db.short_new_entries(period, limit=50)
    squeeze = db.squeeze_ranking(period, limit=50)
    return render_template('short.html',
                           period=period,
                           rank=rank,
                           new_short=new_short,
                           squeeze=squeeze,
                           top_ratio=db.short_top_ratio(50),
                           info=db.short_data_range())


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
    for lst in (by_surge, by_rise, by_fall):
        for p in lst:
            p['name'] = name_map.get(p['code'], '')
    return render_template('rankings.html',
                           by_surge=by_surge, by_rise=by_rise, by_fall=by_fall,
                           hit_ranking=db.get_hit_count_ranking(30))


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
