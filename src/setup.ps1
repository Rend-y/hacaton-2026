# Создаёт изолированное окружение проекта; реальные ключи и готовые результаты сохраняются.
param([switch]$WithPdf)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot '.venv'
$environmentPython = Join-Path $environmentPath 'Scripts/python.exe'

if (-not (Test-Path -LiteralPath $environmentPython)) {
  $basePython = $env:PYTHON_EXECUTABLE
  if (-not $basePython) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand -and $pythonCommand.Source -notlike '*Microsoft\WindowsApps*') {
      $basePython = $pythonCommand.Source
    }
  }
  if (-not $basePython) {
    $bundledPython = 'C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1104-2876-0_26.1104.2876.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
    if (Test-Path -LiteralPath $bundledPython) { $basePython = $bundledPython }
  }
  if (-not $basePython) { throw 'Установите Python 3.10+ либо задайте PYTHON_EXECUTABLE.' }
  & $basePython -m venv $environmentPath
  if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать .venv.' }
}

# Ставим зависимости только в .venv этого проекта, не в системный Python.
& $environmentPython -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
if ($LASTEXITCODE -ne 0) { throw 'Нужен Python 3.10 или новее.' }
& $environmentPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости CLI.' }
if ($WithPdf) {
  & $environmentPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements-pdf.txt')
  if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить поддержку PDF.' }
}

$configPath = Join-Path $projectRoot '.env'
if (-not (Test-Path -LiteralPath $configPath)) {
  Copy-Item -LiteralPath (Join-Path $projectRoot '.env.example') -Destination $configPath
  Write-Output 'Создан .env: укажите в нём API-ключ для генерации.'
}
Write-Output 'Готово. Справка: .\run.ps1 --help'
if ($env:PYTHON_EXECUTABLE -and $env:PYTHON_EXECUTABLE -ne $environmentPython) {
  Write-Output 'PYTHON_EXECUTABLE задан явно и имеет приоритет. Чтобы использовать .venv, удалите эту переменную из текущего окружения.'
}
