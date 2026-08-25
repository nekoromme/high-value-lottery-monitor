@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo RICOH応募準備ツールを起動します。
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo Pythonが見つかりません。
  echo GitHub ActionsからWindows版ZIPをダウンロードして使ってください。
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 初回だけ、PC内へ専用の実行環境を作ります。
  py -3 -m venv .venv
  if errorlevel 1 goto :error
)

if not exist ".venv\.pc-ready" (
  echo 初回だけ、ブラウザ操作部品をインストールします。
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 goto :error
  ".venv\Scripts\python.exe" -m pip install -e ".[pc]"
  if errorlevel 1 goto :error
  echo ready>".venv\.pc-ready"
)

".venv\Scripts\python.exe" -m high_value_lottery_monitor.pc_prepare
set "EXIT_CODE=%ERRORLEVEL%"
echo.
pause
exit /b %EXIT_CODE%

:error
echo.
echo セットアップに失敗しました。上に表示された内容を確認してください。
pause
exit /b 1
