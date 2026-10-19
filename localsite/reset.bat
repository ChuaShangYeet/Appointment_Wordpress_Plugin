@echo off
rem Throws away everything done on the local test site and starts again from the sample data.
cd /d "%~dp0"
echo This deletes every booking, sign-up and change made on the local test site and starts again from the sample data.
choice /m "Continue"
if errorlevel 2 exit /b
call start.bat --reset
