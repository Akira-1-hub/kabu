"""
空売りデータ日次更新（Windowsタスクスケジューラ用）
アプリを開いていなくても、これを毎日実行すればDBに最新分が追加される
"""
import sys
import fetch_jpx

if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    added = fetch_jpx.import_jpx()
    print(f'\n[{__import__("datetime").datetime.now():%Y-%m-%d %H:%M}] 完了: {added}件追加')
