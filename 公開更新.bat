@echo off
chcp 65001 >nul
cd /d C:\Users\akino\kabu
echo === 公開データ生成中 ===
python export_static.py
if errorlevel 1 ( echo エラー: export失敗 & pause & exit /b 1 )
echo.
echo === GitHubへpush中 ===
git add docs
git commit -m "公開データ更新 %date% %time:~0,5%"
git push
echo.
echo 完了！ https://akira-1-hub.github.io/kabu/ に反映されます（1〜2分後）
pause
