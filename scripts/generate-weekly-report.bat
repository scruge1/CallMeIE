@echo off
cd %~dp0

set VAPI_API_KEY=REMOVED_EXPOSED_CREDENTIAL
set ASSISTANT_ID=0b37deb5-2fc2-4e7b-81b1-e61e97103506

python generate-report.py --days 7 --assistant-id %ASSISTANT_ID% --business "Bright Smile Dental" --api-key %VAPI_API_KEY%
