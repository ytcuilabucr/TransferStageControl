@echo off
rem Launch TransferStageControl from the project's virtual environment.
rem Extra arguments are passed through, e.g.  run.bat --config other.yaml
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo Virtual environment .venv not found in %CD%.
    echo Create it first:
    echo     py -3.12 -m venv .venv
    echo     .venv\Scripts\activate
    echo     pip install -e .[dev]
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
python -m transferstage %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
    echo.
    echo TransferStageControl exited with code %RC%. See logs\app.log for details.
    pause
)
endlocal & exit /b %RC%
