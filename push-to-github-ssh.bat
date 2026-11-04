@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM  通过 SSH 推送 main 到 GitHub
REM
REM  首次使用前：把下面显示的公钥添加到 GitHub
REM    https://github.com/settings/keys  ->  New SSH key
REM
REM  用法：
REM    push-to-github-ssh.bat            普通推送（远程已同步时）
REM    push-to-github-ssh.bat force      强制推送（覆盖远程 main）
REM
REM  说明：本地历史由「从零导入」而来，与远程 main 无共同祖先，
REM        首次推送需带 force 参数。
REM ============================================================

set "MODE=%~1"
if /i "%MODE%"=="force" (set "FORCE=--force") else (set "FORCE=")

echo ============================================
echo  推送 main 到 GitHub (SSH^)
echo ============================================
echo.
echo  [检查] SSH 公钥（需已添加到 GitHub）：
echo          https://github.com/settings/keys
if exist "%USERPROFILE%\.ssh\id_ed25519.pub" (
  type "%USERPROFILE%\.ssh\id_ed25519.pub"
) else (
  echo  [ERROR] 未找到 .ssh\id_ed25519.pub，请先运行 ssh-keygen
  pause
  exit /b 1
)
echo.
echo  [1/2] 验证 SSH 通道 ...
ssh -o StrictHostKeyChecking=no -o BatchMode=yes -T git@github.com
echo        ^^ "Hi xiaok1024!" 即成功
echo.
echo  [2/2] 推送 main 分支 %FORCE% ...
git push %FORCE% origin main:main
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo  [OK] 推送成功！
  echo       仓库: https://github.com/xiaok1024/SVN_Sync_Tool
) else (
  echo  [FAIL] 推送失败，退出码 %RC%
  echo         若提示 non-fast-forward，请改用：push-to-github-ssh.bat force
)
echo.
pause
exit /b %RC%
