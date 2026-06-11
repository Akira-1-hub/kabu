@echo off
chcp 65001 >nul
cd /d C:\Users\akino\kabu

echo === 公開サイト生成中（site/） ===
python export_static.py
if errorlevel 1 ( echo エラー: export失敗 & pause & exit /b 1 )

echo.
echo === gh-pages へ公開中（force push） ===
cd site
if exist .git rmdir /s /q .git
git init -q -b gh-pages
git add -A
git commit -q -m "publish %date% %time:~0,5%"
git push -f https://github.com/Akira-1-hub/kabu.git gh-pages
set ERR=%errorlevel%
rmdir /s /q .git
cd ..
if not "%ERR%"=="0" ( echo push失敗 & pause & exit /b 1 )

echo.
echo 完了！ https://akira-1-hub.github.io/kabu/ に1〜2分で反映されます
pause
