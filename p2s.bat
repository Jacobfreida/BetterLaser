@echo off
setlocal
set "HERE=%~dp0"
rem 1) portable Python unzipped into a "python" folder next to this file
if exist "%HERE%python\python.exe" goto portable
rem 2) the Python launcher (py)
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 goto launcher
rem 3) python on PATH (skips the Microsoft Store placeholder)
python -c "import sys" >nul 2>nul
if not errorlevel 1 goto onpath
echo Python was not found. See SETUP-WINDOWS.txt in this folder.
exit /b 1

:portable
"%HERE%python\python.exe" "%HERE%p2s.py" %*
exit /b
:launcher
py -3 "%HERE%p2s.py" %*
exit /b
:onpath
python "%HERE%p2s.py" %*
exit /b
