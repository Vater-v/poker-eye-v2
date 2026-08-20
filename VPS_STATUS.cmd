@echo off
call "%~dp0scripts\vps.cmd" status %*
exit /b %ERRORLEVEL%
