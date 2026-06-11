@echo off
chcp 65001 >nul
cd /d C:\Users\akino\kabu
python -c "import flask" 2>nul || pip install flask
python app.py
pause
