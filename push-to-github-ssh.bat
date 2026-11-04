@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============================================================
REM  通过 SSH 推送 main 到 GitHub（绕开令牌权限问题）
REM
REM  前置条件：把下面显示的公钥添加到 GitHub
REM    https://github.com/settings/keys  ->  New SSH key
REM
REM  说明：本地历史与远程 main 无共同祖先，故使用 --force。
REM ============================================================

set "REPO=git@github.com:xiaok1024/SVN_Sync_Tool.git"

echo ============================================
echo  通过 SSH 推送 main 到 GitHub
echo ============================================
echo.
echo  [前置] 请确认以下公钥已添加到 GitHub：
echo          https://github.com/settings/keys
echo.
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
echo        ^^ 出现 "Hi xiaok1024!" 即成功；publickey 报错说明公钥未添加
echo.
echo  [2/2] 推送 main 分支 ...
git remote remove gh-ssh 2>nul
git remote add gh-ssh "%REPO%"
git push --force gh-ssh main:main
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo  [OK] 推送成功！
  echo       仓库: https://github.com/xiaok1024/SVN_Sync_Tool
) else (
  echo  [FAIL] 推送失败，退出码 %RC%
)
echo.
pause
exit /b %RC%
