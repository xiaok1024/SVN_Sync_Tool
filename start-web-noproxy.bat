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

REM 端口：第一个参数，默认 8765
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"

REM 局域网开关：第二个参数传 local 则仅本机；默认开放局域网
set "MODE=%~2"
if /i "%MODE%"=="local" (
  set "LAN_ARG="
  set "BIND_DESC=仅本机（127.0.0.1）"
) else (
  set "LAN_ARG=--lan"
  set "BIND_DESC=局域网（0.0.0.0）"
)

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

REM 取本机局域网 IP 用于提示
set "LANIP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
  if not defined LANIP set "LANIP=%%a"
)
set "LANIP=%LANIP: =%"

echo ============================================
echo  SVN Sync Tool Web
echo  监听模式: %BIND_DESC%   端口: %PORT%
echo.
echo  本机访问 : http://127.0.0.1:%PORT%/
if defined LANIP echo  内网访问 : http://%LANIP%:%PORT%/
echo.
echo  停止: 关闭本窗口
echo ============================================
echo.

"%PY%" svn_sync_web.py --port %PORT% %LAN_ARG% 1>>"%~dp0run\web-console.out.log" 2>>"%~dp0run\web-console.err.log"

echo.
echo 服务已退出。日志: %~dp0run
pause
