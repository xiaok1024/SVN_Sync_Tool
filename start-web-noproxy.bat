@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM 清空系统代理变量，避免 PowerShell Start-Process 因大小写重名报错
set "http_proxy="
set "https_proxy="
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "NO_PROXY=*"
set "no_proxy=*"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

set "PY=%~dp0.venv-web\Scripts\python.exe"
if not exist "%PY%" (
  echo [ERROR] 未找到虚拟环境: %PY%
  echo 请先执行:
  echo   python -m venv .venv-web
  echo   .venv-web\Scripts\python.exe -m pip install -r requirements-web.txt
  pause
  exit /b 1
)

if not exist "%~dp0run" mkdir "%~dp0run"

echo ============================================
echo  SVN Sync Tool Web
echo  URL : http://127.0.0.1:%PORT%/
echo  停止: 关闭本窗口 或 运行 stop-web.bat
echo ============================================
echo.

"%PY%" svn_sync_web.py --port %PORT% 1>>"%~dp0run\web-console.out.log" 2>>"%~dp0run\web-console.err.log"

echo.
echo 服务已退出。日志: %~dp0run
pause
