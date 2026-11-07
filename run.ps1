# Запускает CLI через установленный Python или Python из Codex на этом компьютере.
$ErrorActionPreference = 'Stop'
$pythonPath = $env:ITFEST_PYTHON
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
  throw 'Install Python 3.10+ or set ITFEST_PYTHON to the full path of python.exe.'
}
$cliArguments = $args
# Пути к данным и результатам остаются предсказуемыми при запуске из любой папки.
Push-Location -LiteralPath $PSScriptRoot
try {
  & $pythonPath (Join-Path $PSScriptRoot 'cli.py') @cliArguments
  $cliExitCode = $LASTEXITCODE
} finally {
  Pop-Location
}
exit $cliExitCode
