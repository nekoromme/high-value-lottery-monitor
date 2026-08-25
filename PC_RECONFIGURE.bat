@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if exist "RICOH_Entry_Assistant.exe" (
  "RICOH_Entry_Assistant.exe" --setup
) else if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m high_value_lottery_monitor.pc_prepare --setup
) else (
  echo 先にRICOH応募準備ツールを一度起動してください。
)

echo.
pause
