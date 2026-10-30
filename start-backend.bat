@echo off
cd /d "%~dp0backend"
echo Starting Cloud Infra Job Portal backend on port 8100...
echo.
python -m uvicorn app.main:app --reload --port 8100
pause
