@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy\windows\stop-web.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Failed to stop SVN Sync Tool Web. Exit code: %EXIT_CODE%
  pause
)
exit /b %EXIT_CODE%
