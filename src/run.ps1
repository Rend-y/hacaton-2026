# Запускает CLI из локального окружения; явный ITFEST_PYTHON имеет приоритет.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = $env:ITFEST_PYTHON
if (-not $pythonPath) {
  $projectPython = Join-Path $projectRoot '.venv/Scripts/python.exe'
  if (Test-Path -LiteralPath $projectPython) { $pythonPath = $projectPython }
}
if (-not $pythonPath) {
  $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
  if ($pythonCommand -and $pythonCommand.Source -notlike '*Microsoft\WindowsApps*') {
    $pythonPath = $pythonCommand.Source
  }
}
if (-not $pythonPath) {
  $bundledPython = 'C:/Program Files/WindowsApps/OpenAI.CodexPrimaryRuntime.v26-1104-2876-0_26.1104.2876.0_x64__3k8sg7r9htsxt/dependencies/python/python.exe'
  if (Test-Path -LiteralPath $bundledPython) { $pythonPath = $bundledPython }
}
if (-not $pythonPath) {
  throw 'Установите Python 3.10+ и выполните .\src\setup.ps1 либо задайте ITFEST_PYTHON.'
}
$cliArguments = $args
# Пути к данным и результатам остаются предсказуемыми при запуске из любой папки.
Push-Location -LiteralPath $projectRoot
try {
  & $pythonPath -m src @cliArguments
  $cliExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}
exit $cliExitCode
