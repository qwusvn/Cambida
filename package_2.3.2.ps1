param([string]$Version = '2.3.2')
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildRoot = Join-Path $Root 'staging\native-build'
New-Item -ItemType Directory -Force -Path "$BuildRoot\temp" | Out-Null
$env:TEMP = "$BuildRoot\temp"
$env:TMP = $env:TEMP
$env:PYINSTALLER_CONFIG_DIR = "$BuildRoot\pyinstaller-cache"
$BuildPython = "$BuildRoot\venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $BuildPython)) {
    & python -m venv --system-site-packages "$BuildRoot\venv"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create build environment' }
    & $BuildPython -m pip install --no-cache-dir --disable-pip-version-check 'Cython==3.1.6'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot install Cython' }
}
& $BuildPython "$Root\scripts\build_native.py" --version $Version
if ($LASTEXITCODE -ne 0) { throw "Native packaging failed: $LASTEXITCODE" }
