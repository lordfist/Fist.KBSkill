param()
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
# Use this PowerShell's built-ins, not an inherited PowerShell 7 module path.
$env:PSModulePath = Join-Path $PSHOME 'Modules'
$packageRoot = Split-Path -Parent $PSScriptRoot
$runtimeRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'FistKB\runtime'
if ($env:FIST_KB_RUNTIME_DIR) { $runtimeRoot = [IO.Path]::GetFullPath($env:FIST_KB_RUNTIME_DIR) }
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCommand) {
    $uvPath = $uvCommand.Source
} else {
    $uvPath = Join-Path $runtimeRoot 'bin\uv.exe'
    if (-not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
        [Console]::Error.WriteLine('Fist KB: preparing uv in its private runtime folder. First launch needs internet access.')
        New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
        $installerPath = Join-Path $runtimeRoot ('uv-install-' + [Guid]::NewGuid().ToString('N') + '.ps1')
        try {
            Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/0.13.5/install.ps1' -OutFile $installerPath
            $env:UV_INSTALL_DIR = Join-Path $runtimeRoot 'bin'
            $env:UV_NO_MODIFY_PATH = '1'
            try {
                $ErrorActionPreference = 'Continue'
                $installerMessages = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $installerPath 2>&1
                $installerExit = $LASTEXITCODE
            } finally { $ErrorActionPreference = 'Stop' }
            foreach ($message in $installerMessages) { [Console]::Error.WriteLine([string]$message) }
            if ($installerExit -ne 0 -or -not (Test-Path -LiteralPath $uvPath -PathType Leaf)) {
                throw 'uv preparation failed. No KB was selected or changed.'
            }
        } finally {
            if (Test-Path -LiteralPath $installerPath) { Remove-Item -LiteralPath $installerPath -Force }
        }
    }
}
$identity = [Text.Encoding]::UTF8.GetBytes($packageRoot + (Get-Content -LiteralPath (Join-Path $packageRoot 'pyproject.toml') -Raw))
$hasher = [Security.Cryptography.SHA256]::Create()
try { $key = ([BitConverter]::ToString($hasher.ComputeHash($identity))).Replace('-', '').Substring(0, 16) }
finally { $hasher.Dispose() }
# Own the runtime selection rather than borrowing a host or another plugin's venv.
# The desktop integration uses the exact Python version exercised by its release tests.
$env:UV_PROJECT_ENVIRONMENT = Join-Path $runtimeRoot ('envs\' + $key)
$env:UV_PYTHON_INSTALL_DIR = Join-Path $runtimeRoot 'python'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_PYTHON = '3.12.15'
$env:UV_PYTHON_NO_REGISTRY = '1'
if (-not $env:UV_CACHE_DIR) { $env:UV_CACHE_DIR = Join-Path $runtimeRoot 'cache' }
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:FIST_KB_PLUGIN_ROOT = $packageRoot
$env:FIST_KB_RUNTIME_DIR = $runtimeRoot
& $uvPath run --locked --python 3.12.15 --directory $packageRoot fist-kb --project-mode mcp
exit $LASTEXITCODE
