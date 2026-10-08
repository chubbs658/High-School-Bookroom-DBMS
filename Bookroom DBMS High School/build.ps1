<#
  build.ps1

  Builds the MHS Bookroom DBMS into a standalone Windows .exe with
  PyInstaller. Run this from the project root (the folder containing
  app.py, common.py, library.py, templates\, and static\).

  Produces dist\MHSBookroom\MHSBookroom.exe (an --onedir build: a folder,
  not a single file). --onedir is used deliberately rather than --onefile:
  --onefile re-extracts its bundled templates/static into a fresh temp
  folder on every single launch, which is slower to start and is exactly
  the kind of folder app.py's database logic now specifically avoids
  writing to. --onedir starts faster and gives you one stable folder to
  actually deploy (copy the whole dist\MHSBookroom\ folder to the target
  PC - not just the .exe file inside it).
#>

$ErrorActionPreference = "Stop"

# 1. Make sure dependencies (including PyInstaller itself) are installed.
#    If you're using a virtual environment, activate it before running
#    this script.
pip install -r requirements.txt

# 2. Clean any previous build output so stale files never linger into a
#    new build.
Remove-Item -Recurse -Force .\build, .\dist, .\MHSBookroom.spec -ErrorAction SilentlyContinue

# 3. Build. --add-data bundles the templates/ and static/ folders (Flask
#    needs both at runtime; PyInstaller does not include non-.py files
#    automatically). On Windows the --add-data separator is a semicolon
#    (SOURCE;DEST) - on macOS/Linux it would be a colon instead.
#
#    --noconsole hides the background command-window a librarian would
#    otherwise see behind the browser tab. Remove --noconsole for your
#    FIRST test build so you can actually see any startup errors; add it
#    back once you've confirmed everything works.
pyinstaller `
    --onedir `
    --noconsole `
    --name MHSBookroom `
    --add-data "templates;templates" `
    --add-data "static;static" `
    app.py

Write-Host ""
Write-Host "Build complete: dist\MHSBookroom\MHSBookroom.exe" -ForegroundColor Green
Write-Host "Deploy by copying the ENTIRE dist\MHSBookroom\ folder to the target machine." -ForegroundColor Cyan
Write-Host "Avoid Program Files / other admin-protected locations - the app writes" -ForegroundColor Cyan
Write-Host "bookroom.db, secret_key.txt, and a backups\ folder next to the .exe," -ForegroundColor Cyan
Write-Host "and needs that folder to be writable by whoever runs it." -ForegroundColor Cyan
