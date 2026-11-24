@echo off
setlocal
where py.exe >nul 2>&1
if errorlevel 1 goto use_python
py.exe -3.13 -c "import sys" >nul 2>&1
if errorlevel 1 goto use_python
py.exe -3.13 -u "%~dp0run_familycart.py" %*
goto done

:use_python
where python.exe >nul 2>&1
if errorlevel 1 goto missing_python
python.exe -u "%~dp0run_familycart.py" %*
goto done

:missing_python
echo Install Python 3.13 to run FamilyCart.
pause
exit /b 1

:done
set "launch_status=%errorlevel%"
if not "%launch_status%"=="0" pause
exit /b %launch_status%
