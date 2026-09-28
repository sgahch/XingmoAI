param([int]$Port = 5178)

$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
python .\server\app.py --port $Port
