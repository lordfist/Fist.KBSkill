param()
# Session-start hook: print the selected project's Fist KB core for the host to add to context.
# Read-only. It uses the private runtime that launch.ps1 prepared for the MCP server and never
# installs uv, Python or packages or contacts the network. Without that runtime, or on any
# failure, it adds nothing and exits 0; the working skill then loads core through kb_core.
$ErrorActionPreference = 'Stop'
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $OutputEncoding = [Console]::OutputEncoding
    $env:PSModulePath = Join-Path $PSHOME 'Modules'
    $packageRoot = Split-Path -Parent $PSScriptRoot
    $runtimeRoot = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'FistKB\runtime'
    if ($env:FIST_KB_RUNTIME_DIR) { $runtimeRoot = [IO.Path]::GetFullPath($env:FIST_KB_RUNTIME_DIR) }
    # launch.ps1 keys its environment by the package root text plus pyproject.toml. Hosts may pass
    # the root with either separator, so try each spelling of the same root.
    $pyproject = Get-Content -LiteralPath (Join-Path $packageRoot 'pyproject.toml') -Raw
    $python = $null
    foreach ($root in @($packageRoot, $packageRoot.Replace('/', '\'), $packageRoot.Replace('\', '/')) | Select-Object -Unique) {
        $identity = [Text.Encoding]::UTF8.GetBytes($root + $pyproject)
        $hasher = [Security.Cryptography.SHA256]::Create()
        try { $key = ([BitConverter]::ToString($hasher.ComputeHash($identity))).Replace('-', '').Substring(0, 16) }
        finally { $hasher.Dispose() }
        $candidate = (Join-Path $runtimeRoot ('envs\' + $key)) + '\Scripts\python.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { $python = $candidate; break }
    }
    if (-not $python) { exit 0 }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $env:FIST_KB_PLUGIN_ROOT = $packageRoot
    $env:FIST_KB_RUNTIME_DIR = $runtimeRoot
    & $python -X utf8 -B -P -m fist_kb session-context
} catch {
    [Console]::Error.WriteLine('Fist KB session context unavailable: ' + $_.Exception.Message)
}
exit 0
