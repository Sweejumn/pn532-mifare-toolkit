@echo off
cd /d "%~dp0"
echo ==========================================================
echo   HardNested 一键破解
echo   前提：PN532 已插好，卡已放在读卡器上
echo ==========================================================
echo.
python hardnested.py auto %*
echo.
pause
