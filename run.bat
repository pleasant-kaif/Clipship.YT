@echo off
SETLOCAL ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

:: ─────────────────────────────────────────────────────────────────────────────
:: 1. Add Ollama to PATH for this session (no system-wide change needed)
:: ─────────────────────────────────────────────────────────────────────────────
set "OLLAMA_EXE=D:\OllamaApp\ollama.exe"
set "PATH=D:\OllamaApp;%PATH%"

:: ─────────────────────────────────────────────────────────────────────────────
:: 2. Load .env variables into this session
::    Lines starting with # or blank are skipped.
::    Real environment variables already set take priority (no override).
:: ─────────────────────────────────────────────────────────────────────────────
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%A in (`findstr /v "^#" .env ^| findstr /v "^$"`) do (
        if not defined %%A set "%%A=%%B"
    )
)

:: Apply defaults if still unset after .env
if not defined OLLAMA_BASE_URL set "OLLAMA_BASE_URL=http://127.0.0.1:11434"
if not defined LOCAL_AI_MODEL  set "LOCAL_AI_MODEL=llama3"
if not defined PORT            set "PORT=5000"

echo.
echo  Clipship.YT — Ollama connection settings
echo  ==========================================
echo  OLLAMA_BASE_URL : %OLLAMA_BASE_URL%
echo  LOCAL_AI_MODEL  : %LOCAL_AI_MODEL%
echo.

:: ─────────────────────────────────────────────────────────────────────────────
:: 3. Start Ollama serve if it is not already running on port 11434
:: ─────────────────────────────────────────────────────────────────────────────
if not exist "%OLLAMA_EXE%" (
    echo [WARNING] Ollama not found at %OLLAMA_EXE%
    echo           SEO descriptions and comment replies will be unavailable.
    echo           Download Ollama from https://ollama.com and install it.
    echo.
    goto setup_python
)

:: Check if Ollama API is already up
curl -s --max-time 1 "%OLLAMA_BASE_URL%/api/tags" >nul 2>&1
if %errorlevel% equ 0 (
    echo [OK] Ollama is already running at %OLLAMA_BASE_URL%
) else (
    echo [INFO] Starting Ollama in the background...
    start "" /B "%OLLAMA_EXE%" serve
    :: Wait up to 8 seconds for the API to become available
    set OLLAMA_READY=0
    for /l %%i in (1,1,8) do (
        if "!OLLAMA_READY!"=="0" (
            timeout /t 1 /nobreak >nul
            curl -s --max-time 1 "%OLLAMA_BASE_URL%/api/tags" >nul 2>&1
            if !errorlevel! equ 0 set OLLAMA_READY=1
        )
    )
    if "!OLLAMA_READY!"=="1" (
        echo [OK] Ollama started successfully.
    ) else (
        echo [WARNING] Ollama did not respond within 8 seconds.
        echo           SEO descriptions and comment replies may be unavailable.
    )
)

:: ─────────────────────────────────────────────────────────────────────────────
:: 4. Set up Python venv and install dependencies
:: ─────────────────────────────────────────────────────────────────────────────
:setup_python
echo.
if not exist ".venv\Scripts\python.exe" (
    echo Creating the Clipship.YT environment...
    python -m venv .venv
    if errorlevel 1 goto failed
)
echo Installing or updating Clipship.YT dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

:: ─────────────────────────────────────────────────────────────────────────────
:: 5. Launch the app
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo Clipship.YT is starting at http://127.0.0.1:%PORT%
echo Press Ctrl+C to stop the local server.
".venv\Scripts\python.exe" app.py
goto end

:failed
echo.
echo Setup failed. Check your Python installation and internet connection.
pause

:end
