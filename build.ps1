$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv (Join-Path $project '.venv')
}

& $python -m pip install --disable-pip-version-check -r (Join-Path $project 'requirements.txt') pyinstaller
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }

& $python -m PyInstaller --noconfirm --clean --onefile --windowed --name Velox `
    --icon (Join-Path $project 'Velox.ico') `
    --add-data "$(Join-Path $project 'web');web" `
    --add-data "$(Join-Path $project 'profiles');profiles" `
    --add-data "$(Join-Path $project 'third_party');third_party" `
    --paths $project --distpath (Join-Path $project 'dist') `
    --workpath (Join-Path $project 'build') `
    --specpath $project (Join-Path $project 'main.py')
if ($LASTEXITCODE -ne 0) { throw 'Сборка Velox.exe не удалась' }

Write-Host "Готово: $(Join-Path $project 'dist\Velox.exe')"
