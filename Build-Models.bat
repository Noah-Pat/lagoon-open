@echo off
rem Rebuilds the Blender models and embeds them in lagoon-open-3d-models.html (the test version).
rem   Build-Models.bat              rebuild everything from models\build_models.py
rem   Build-Models.bat --from-blend export your own edits saved in models\lagoon-models.blend
setlocal
cd /d "%~dp0"
set "BLENDER="
for /d %%D in ("%ProgramFiles%\Blender Foundation\Blender*") do if exist "%%D\blender.exe" set "BLENDER=%%D\blender.exe"
if not defined BLENDER (
  where blender >nul 2>nul && set "BLENDER=blender"
)
if not defined BLENDER (
  echo Blender was not found. Install it from https://www.blender.org/download/
  pause
  exit /b 1
)
echo Using "%BLENDER%"
"%BLENDER%" -b --factory-startup --python-exit-code 1 -P "models\build_models.py" -- %*
if errorlevel 1 (echo Build failed. & pause & exit /b 1)
if exist "models\lagoon-models.blend1" del "models\lagoon-models.blend1"
echo Done. Reload lagoon-open-3d-models.html to see the models.
pause
