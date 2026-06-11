# 投資DB（株式分析ダッシュボード）

日本株の株価・出来高・空売り残高を毎日蓄積して、ブラウザで分析するローカルWebアプリ。

## 機能

- **スキャン**: kabutanから全銘柄（約3,500）の株価・出来高を取得しSQLiteに蓄積
- **スクリーニング**: 前日比・出来高倍率で即時絞り込み（再スキャン不要）
- **空売り分析**: JPX公式の空売り残高を毎日自動追加
  - 増加 / 減少 / 新規 ランキング（デイリー・週間・今週）
  - 残高割合トップ、機関別内訳、銘柄ごとの推移チャート
- **銘柄詳細**: 株価・出来高・空売りチャート、企業情報（時価総額/PER/PBR/EPS/事業内容）
- **ウォッチリスト・メモ・CSV出力**

## 起動

```
投資DB起動.bat をダブルクリック
→ http://localhost:5000 が開く
```

または:

```bash
pip install flask requests pandas beautifulsoup4 lxml openpyxl xlrd
python app.py
```

## 構成

| ファイル | 役割 |
|---|---|
| `app.py` | Flask本体・ルート |
| `db.py` | SQLiteスキーマ・集計クエリ |
| `fetch.py` | kabutan株価スキャン・企業情報取得 |
| `fetch_jpx.py` | JPX空売り残高の自動取得（機関名の名寄せ込み）|
| `import_short.py` | 空売りネット形式Excelの一括取込 |
| `update_short.py` | 空売り日次更新（タスクスケジューラ用）|
| `get_all_codes.py` | JPX上場銘柄マスタ更新 |
| `backfill_names.py` | 上場廃止銘柄の名前補完 |
| `templates/` `static/` | 画面 |

## データ

- `stocks.db`（SQLite・git管理外）に全データを蓄積
- 空売り残高: 機関別・日次、繰り越し方式で現在残高を算出（報告閾値0.5%）
