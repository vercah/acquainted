@echo off
setlocal
cd /d "%~dp0"

if not exist .venv (
    echo First run: creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo Failed to create venv. Is Python installed and on PATH?
        pause
        exit /b 1
    )
    call .venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install dependencies.
        pause
        exit /b 1
    )
) else (
    call .venv\Scripts\activate.bat
)

start "" http://127.0.0.1:5000/
python app\app.py

endlocal
