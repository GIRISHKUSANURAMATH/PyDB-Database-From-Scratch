@echo off
rem PyDB: set up the virtual environment and verify everything on Windows.
rem Usage: double-click this file, or run  scripts\windows_verify.bat  from the project root.
rem All output also goes to data\windows_verify.log
setlocal
cd /d "%~dp0.."
set "LOG=%CD%\data\windows_verify.log"
call :main > "%LOG%" 2>&1
type "%LOG%"
echo.
echo Log written to %LOG%
if not "%PYDB_NO_PAUSE%"=="1" pause
exit /b %RESULT%

:main
set RESULT=0
echo ===== PyDB Windows verification  %DATE% %TIME%
echo ----- environment
ver
echo PROJECT=%CD%
where.exe python
where.exe py
where.exe git
python --version
python -m pip --version
git --version
echo.
echo ----- virtual environment
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  echo venv_create_exit=%ERRORLEVEL%
)
".venv\Scripts\python.exe" --version
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
echo pip_install_exit=%ERRORLEVEL%
echo.
echo ----- pytest
".venv\Scripts\python.exe" -m pytest -v -rA
set PYTEST_EXIT=%ERRORLEVEL%
echo pytest_exit=%PYTEST_EXIT%
if not "%PYTEST_EXIT%"=="0" set RESULT=1
echo.
echo ----- ruff
".venv\Scripts\python.exe" -m ruff check .
set RUFF_EXIT=%ERRORLEVEL%
echo ruff_exit=%RUFF_EXIT%
if not "%RUFF_EXIT%"=="0" set RESULT=1
echo.
echo ----- end-to-end checks
".venv\Scripts\python.exe" scripts\windows_verify.py
set E2E_EXIT=%ERRORLEVEL%
echo e2e_exit=%E2E_EXIT%
if not "%E2E_EXIT%"=="0" set RESULT=1
echo.
echo ----- benchmarks (5000 rows)
".venv\Scripts\python.exe" benchmarks\run_benchmarks.py --rows 5000 --lookups 200 --out data\benchmark_windows.md
echo bench_exit=%ERRORLEVEL%
echo.
echo ----- git
git status --short --branch
git log --oneline -n 5
git config user.name
git config user.email
where.exe gh
gh auth status
echo.
echo ===== FINAL RESULT=%RESULT%  (pytest=%PYTEST_EXIT% ruff=%RUFF_EXIT% e2e=%E2E_EXIT%)
exit /b 0
