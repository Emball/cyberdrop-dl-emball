@echo off
REM User defined variables
set "COMMANDLINE_ARGS="
set "AUTO_UPDATE=true"

REM ----------------------------------------------------------
set "GITHUB_REPO=Emball/cyberdrop-dl-emball"
set "PACKAGE_NAME=cyberdrop-dl-patched"
set "INSTALL_NAME=cyberdrop-dl-emball"

if /i "%PROCESSOR_ARCHITECTURE%"=="x86" (
    echo ERROR: 32-bit Windows is not supported.
    pause
    exit /b 1
)

where uv >nul 2>&1
if errorlevel 1 (
    echo uv not found, installing...
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/0.10.11/install.ps1 | iex"
    if errorlevel 1 (
        echo Error: Failed to install uv.
        pause
        exit /b 1
    )
    uv tool update-shell
)

set "PACKAGE_INSTALLED=false"
where %PACKAGE_NAME% >nul 2>&1
if %errorlevel%==0 (
    set "PACKAGE_INSTALLED=true"
)

if "%AUTO_UPDATE%"=="true" (
    goto :INSTALL_OR_UPDATE
)

if "%PACKAGE_INSTALLED%"=="false" (
    goto :INSTALL_OR_UPDATE
)
goto :RUN

:INSTALL_OR_UPDATE
echo Installing / Updating %INSTALL_NAME% from GitHub...
pip uninstall cyberdrop-dl cyberdrop-dl-patched -qq >nul 2>&1
uv tool install --managed-python -p ">=3.12,<3.14" --upgrade "cyberdrop-dl-patched @ git+https://github.com/%GITHUB_REPO%.git"
if errorlevel 1 (
    echo Error: Failed to install %INSTALL_NAME%.
    pause
    exit /b 1
)

:RUN
echo Starting %INSTALL_NAME%...
%PACKAGE_NAME% %COMMANDLINE_ARGS%
pause
