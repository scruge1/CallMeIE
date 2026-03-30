@echo off
echo ==========================================
echo   AI Receptionist Demo Recorder
echo ==========================================
echo.
echo This will:
echo   1. Open the Vapi assistant in a browser
echo   2. Start recording video
echo   3. Click "Talk" to begin a live call
echo   4. Record your conversation with Sarah
echo.
echo Press Ctrl+C when you're done talking.
echo.
echo Starting in 3 seconds...
timeout /t 3 /nobreak >nul
python "%~dp0record-demo.py"
echo.
echo Demo saved to Desktop\ai-agency\demo\
pause
