@echo off
cd /d C:\Users\akino\kabu
python -c "import flask" 2>nul || pip install flask
echo.
echo === 投資DB起動中 ===
echo ブラウザで http://localhost:5000 を開いてください
echo （この黒い窓を閉じるとツールは止まります）
echo.
python app.py
pause
