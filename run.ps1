$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'src/run.ps1') @args
exit $LASTEXITCODE
