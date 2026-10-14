@echo off
setlocal enabledelayedexpansion
pushd "%~dp0"

echo =======================================================
echo [INFO] MASTER QA LAUNCHER STARTED
echo =======================================================
echo.

REM Load .env (KEY=value lines, # comments); system variables take precedence
if exist ".env" for /f "usebackq eol=# tokens=1,* delims==" %%a in (".env") do if not defined %%a set "%%a=%%b"

REM LanguageTool install folder; override with LT_DIR in .env or the environment
if not defined LT_DIR set "LT_DIR=C:\LanguageTool\LanguageTool-6.4"

REM =======================================================
REM 1) CHECK IF LanguageTool SERVER IS RUNNING ON 8081
REM =======================================================
echo [INFO] Checking if LanguageTool server is already running on port 8081...

netstat -ano | findstr ":8081" | findstr "LISTENING" > lt_check.tmp
if %ERRORLEVEL%==0 (
    echo [INFO] LanguageTool server is already running.
) else (
    echo [INFO] No server detected. Starting LanguageTool now...
    start "LanguageTool" cmd /K ^
        "cd /d "%LT_DIR%" && java -cp .;languagetool.jar;languagetool-server.jar org.languagetool.server.HTTPServer --port 8081"
    echo [INFO] Waiting 5 seconds for server to start...
    timeout /t 5 >nul
)
del lt_check.tmp 2>nul
echo.

REM =======================================================
REM 2) CHECK VENV
REM =======================================================
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found!
    echo Create using:
    echo     py -3.11 -m venv .venv
    echo     .\.venv\Scripts\activate
    echo     pip install -r requirements.txt
    echo.
    pause
    popd
    exit /b 1
)

echo [INFO] Activating virtual environment...
call ".venv\Scripts\activate.bat"
echo.

REM =======================================================
REM 3) RUN AUTO_QA.PY
REM =======================================================
echo [INFO] Running AutoQA...
echo.

python auto_qa.py
set EXITCODE=%ERRORLEVEL%

echo.
echo [INFO] AutoQA finished with exit code %EXITCODE%.
echo =======================================================

pause
popd
endlocal
exit /b %EXITCODE%
