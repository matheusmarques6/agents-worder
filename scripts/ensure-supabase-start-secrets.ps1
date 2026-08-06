$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$keyDir = Join-Path $repoRoot "supabase\.temp\start-secrets\supabase_db_agents-worder"
$keyPath = Join-Path $keyDir "secret-0"

New-Item -ItemType Directory -Path $keyDir -Force | Out-Null

if (Test-Path -LiteralPath $keyPath -PathType Container) {
    throw "Expected a file at '$keyPath', but found a directory."
}

if (-not (Test-Path -LiteralPath $keyPath -PathType Leaf)) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    }
    finally {
        $rng.Dispose()
    }
    $hex = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Set-Content -LiteralPath $keyPath -Value $hex -NoNewline -Encoding ascii
}

Write-Host "Ensured $keyPath"
