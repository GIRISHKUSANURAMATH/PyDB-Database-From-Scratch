@echo off
rem PyDB: create the Git repository (branch main) and the initial commit.
rem Author/committer = your configured git user.name / user.email. Output: data\git_setup.log
setlocal
cd /d "%~dp0.."
set "LOG=%CD%\data\git_setup.log"
call :main > "%LOG%" 2>&1
type "%LOG%"
echo.
echo Log written to %LOG%
if not "%PYDB_NO_PAUSE%"=="1" pause
exit /b

:main
echo ===== PyDB git setup  %DATE% %TIME%
git --version
for /f "delims=" %%a in ('git config user.name') do set "GNAME=%%a"
for /f "delims=" %%a in ('git config user.email') do set "GMAIL=%%a"
echo user.name=%GNAME%
echo user.email=%GMAIL%
if "%GNAME%"=="" ( echo ABORT: git user.name is not set & exit /b 1 )
if "%GMAIL%"=="" ( echo ABORT: git user.email is not set & exit /b 1 )
if not exist ".git" (
  git init -b main
) else (
  echo existing repository found:
  git log --oneline -n 5
)
git add -A
echo ----- staged files
git diff --cached --name-only
git diff --cached --name-only | findstr /i /r "\.db$ \.wal$ \.lock$ \.venv/ __pycache__ \.log$" && ( echo ABORT: generated files are staged & exit /b 1 )
echo ----- whitespace check
git diff --cached --check
if errorlevel 1 ( echo ABORT: git diff --cached --check found problems & exit /b 1 )
echo whitespace_check=ok
git rev-parse --verify HEAD >nul 2>&1
if errorlevel 1 (
  git commit -m "Initial commit: PyDB database engine"
) else (
  git diff --cached --quiet && echo nothing new to commit || git commit -m "Update PyDB"
)
git branch -M main
echo ----- verify
git log -1 --format="commit=%%H%%nauthor=%%an <%%ae>%%ncommitter=%%cn <%%ce>%%nsubject=%%s"
git status --short --branch
git remote -v
git ls-files | find /c /v ""
echo ===== DONE
exit /b 0
