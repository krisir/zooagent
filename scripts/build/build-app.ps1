# Windows 入口：真正的逻辑在 build-app.py，两端共用一份。
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here "build-app.py"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $script @args
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $script @args
} else {
    Write-Error "需要 Python 3：winget install --id Python.Python.3.12"
}

exit $LASTEXITCODE
