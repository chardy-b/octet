@echo off
setlocal
set "TIDAL_PYTHON=%~dp0..\..\.venv-tidal\Scripts\python.exe"
if not exist "%TIDAL_PYTHON%" (
  echo Run tools\tidal\install-local.ps1 first. 1>&2
  exit /b 1
)
"%TIDAL_PYTHON%" "%~dp0tidal_cli.py" %*
exit /b %errorlevel%
