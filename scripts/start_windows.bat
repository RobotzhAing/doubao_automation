@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONDONTWRITEBYTECODE=1"
cd /d "%~dp0.." || exit /b 1
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -u scripts\windows_bootstrap.py
  goto finished
)
where py.exe >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
  if not errorlevel 1 (
    py -3 -u scripts\windows_bootstrap.py
    goto finished
  )
)
where python.exe >nul 2>&1
if not errorlevel 1 (
  python -c "import sys; sys.exit(sys.version_info < (3,10))" >nul 2>&1
  if not errorlevel 1 (
    python -u scripts\windows_bootstrap.py
    goto finished
  )
)
echo 请先安装 Python 3.10 或更新版本并添加到 PATH，然后重新双击。
if not defined CI pause
exit /b 1
:finished
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
  echo 启动失败，请查看上方提示与启动日志。
  if not defined CI pause
)
exit /b %RESULT%
