@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  推送到 Gitee
REM  用法：
REM    push-to-gitee.bat 你的Gitee私人令牌
REM  或先设环境变量再运行：
REM    set GITEE_TOKEN=xxxxx && push-to-gitee.bat
REM
REM  令牌可在 https://gitee.com/profile/personal_access_tokens 生成
REM  需要勾选权限：projects（仓库读写）
REM ============================================================

set "TOKEN=%~1"
if "%TOKEN%"=="" set "TOKEN=%GITEE_TOKEN%"
if "%TOKEN%"=="" (
  echo [ERROR] 未提供 Gitee 私人令牌。
  echo.
  echo 用法: push-to-gitee.bat ^<你的令牌^>
  echo 生成地址: https://gitee.com/profile/personal_access_tokens
  echo.
  pause
  exit /b 1
)

set "USER=xiaok1024"
set "REPO=SVN_Sync_Tool"
set "BRANCH=main"

REM 清掉大小写重名的代理变量，避免干扰
set "http_proxy="
set "https_proxy="
set "HTTP_PROXY="
set "HTTPS_PROXY="

echo ============================================
echo  推送 %REPO% 到 Gitee (%USER/%BRANCH%)
echo ============================================
echo.

git -c credential.helper= push "https://%USER%:%TOKEN%@gitee.com/%USER%/%REPO%.git" %BRANCH%:%BRANCH%
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo [OK] 推送成功。
  echo 仓库地址: https://gitee.com/%USER%/%REPO%
) else (
  echo [FAIL] 推送失败，退出码 %RC%
  echo.
  echo 常见原因：
  echo   1. Gitee 上还没有 %USER%/%REPO% 这个仓库 -^> 先去网页新建（不要勾选初始化 README）
  echo   2. 令牌无效或权限不足（需勾选 projects 权限）
  echo   3. 令牌已过期
)
echo.
pause
exit /b %RC%
