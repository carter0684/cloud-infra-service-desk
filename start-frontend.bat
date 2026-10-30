@echo off
cd /d "%~dp0frontend"
echo Installing dependencies...
call npm install
echo.
echo Starting Cloud Infra Job Portal frontend on port 5200...
echo.
npm run dev
pause
