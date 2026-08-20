@echo off
call "%~dp0scripts\vps.cmd" logs %*
exit /b %ERRORLEVEL%
