@echo off
:: Run inventory sync every 5 minutes
:: Usage: Run this once to create the scheduled task
:: Or double-click to run a manual sync

cd %~dp0

:: Set your config here
set VAPI_API_KEY=69a708ae-229f-4d0b-bb37-ac4e9ecd2afb
set SHEET_ID=YOUR_GOOGLE_SHEET_ID_HERE
set ASSISTANT_ID=0b37deb5-2fc2-4e7b-81b1-e61e97103506

python sync-inventory.py --sheet-id %SHEET_ID% --assistant-id %ASSISTANT_ID% --api-key %VAPI_API_KEY%
