@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

REM ============================================================
REM  一键推送到 Gitee（自动创建仓库，若不存在）
REM
REM  用法：
REM    push-to-gitee.bat <你的Gitee私人令牌>
REM
REM  令牌生成：https://gitee.com/profile/personal_access_tokens
REM  需要勾选：projects（仓库读写）
REM ============================================================

set "TOKEN=%~1"
if "%TOKEN%"=="" set "TOKEN=%GITEE_TOKEN%"
if "%TOKEN%"=="" (
  echo [ERROR] 未提供 Gitee 私人令牌。
  echo.
  echo 用法:  push-to-gitee.bat ^<你的令牌^>
  echo 生成:  https://gitee.com/profile/personal_access_tokens
  echo 权限:  勾选 projects
  echo.
  pause
  exit /b 1
)

set "OWNER=xiaok1024"
set "REPO=SVN_Sync_Tool"
set "BRANCH=main"
set "PY=%~dp0.venv-web\Scripts\python.exe"

REM 清掉大小写重名的代理变量，避免干扰
set "http_proxy="
set "https_proxy="
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "NO_PROXY=*"
set "no_proxy=*"

echo ============================================
echo  推送 %OWNER%/%REPO% 到 Gitee
echo ============================================
echo.

if not exist "%PY%" (
  echo [ERROR] 未找到虚拟环境: %PY%
  pause
  exit /b 1
)

echo [1/2] 准备远程仓库 ...
"%PY%" "%~dp0gitee_create_repo.py" "%TOKEN%" "%OWNER%" "%REPO%"
if errorlevel 1 (
  echo.
  echo [FAIL] 远程仓库不可用，推送中止。
  echo        请确认令牌已勾选 projects 权限。
  pause
  exit /b 1
)

echo.
echo [2/2] 推送 %BRANCH% 分支 ...
git -c credential.helper= push "https://%OWNER%:%TOKEN%@gitee.com/%OWNER%/%REPO%.git" %BRANCH%:%BRANCH%
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
  echo [OK] 推送成功！
  echo      仓库地址: https://gitee.com/%OWNER%/%REPO%
) else (
  echo [FAIL] 推送失败，退出码 %RC%
  echo        常见原因：令牌无效/过期、权限不足。
)
echo.
pause
exit /b %RC%
