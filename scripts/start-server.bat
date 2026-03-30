@echo off
echo Starting AI Receptionist Server...
echo.

:: Start the webhook server
start "AI-Server" cmd /c "cd %~dp0 && python server.py"

:: Wait for server to start
timeout /t 3 /nobreak >/dev/null

:: Start localtunnel
echo Starting public tunnel...
start "AI-Tunnel" cmd /c "npx localtunnel --port 8080 --print-requests"

echo.
echo Server running on http://localhost:8080
echo Tunnel URL will appear in the tunnel window.
echo.
echo Press any key to stop both...
pause

:: Kill both
taskkill /FI "WINDOWTITLE eq AI-Server*" /F 2>/dev/null
taskkill /FI "WINDOWTITLE eq AI-Tunnel*" /F 2>/dev/null
