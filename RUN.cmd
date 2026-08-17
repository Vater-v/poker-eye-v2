@echo off
setlocal
cd /d "%~dp0"
if "%POKEREYE_V2_SECRET%"=="" if exist "secrets\trainer.secret" set /p POKEREYE_V2_SECRET=<"secrets\trainer.secret"
if "%POKEREYE_V2_SECRET%"=="" (
  echo [ERROR] Set POKEREYE_V2_SECRET or create secrets\trainer.secret before starting trainer.
  exit /b 2
)
python main.py --secret "%POKEREYE_V2_SECRET%" %*
endlocal
