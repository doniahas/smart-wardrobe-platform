<#
.SYNOPSIS
    Native Windows PowerShell companion to scripts/run_tests_docker.sh.

.DESCRIPTION
    scripts/run_tests_docker.sh - the primary, Docker-only test command -
    requires Git Bash or WSL on Windows. This script provides the exact
    same behavior natively in PowerShell, so a plain Windows + Docker
    Desktop + PowerShell machine needs nothing else.

    This is NOT a separate testing mechanism and does not add, modify, weaken,
    or duplicate a single test - it runs the exact same command shape as
    the Bash version: a disposable `python:3.11-slim` container per
    service, with the repository root bind-mounted, that service's own
    directory as the container's working directory, `TESTING=true` set,
    a fresh `pip install -r requirements.txt` (no image build required),
    then that service's own unmodified `pytest` suite - unit,
    integration, security, and stress/concurrency tests all together,
    exactly as `cd services/<name> && pytest` already runs them (see
    docs/testing.md).

    What this does NOT run: tests/system/ (the opt-in System/E2E suites -
    see docs/testing.md's "System / end-to-end tests" section; not
    automated here, on either platform, by design).

    Non-destructive and deterministic: every suite already runs against
    mongomock-motor/fakeredis/mocked HTTP - no real Mongo, Redis, network
    access, or the already-running `docker compose` stack (if any) is
    touched. Each service runs in its own `--rm` container; nothing is
    left running afterward. The repository is bind-mounted read-write,
    but no test in any of these four suites writes outside a pytest-
    provided tmp_path (see e.g. services/web/tests/test_explore_media.py's
    own tmp_path-backed upload-dir fixture), so the working tree is left
    unchanged.

.PARAMETER Services
    Optional list of service names to run (recommendation, web, llm,
    catalog). Defaults to all four when omitted.

.EXAMPLE
    .\scripts\run_tests_docker.ps1
    Runs all four services' test suites.

.EXAMPLE
    .\scripts\run_tests_docker.ps1 web catalog
    Runs only the web and catalog suites.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Services = @()
)

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location -Path $RepoRoot

# --- Docker availability checks -------------------------------------------
# Same early, actionable checks as run_tests_docker.sh, rather than a
# confusing failure deep inside the first `docker run`.

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: docker is not available on PATH." -ForegroundColor Red
    Write-Host "See README.md's `"Prerequisites`" section."
    exit 1
}

# `*> $null` (redirect ALL streams, not `2>&1`) avoids PowerShell 5.1's
# NativeCommandError wrapping of a native executable's stderr output -
# $LASTEXITCODE is checked explicitly below regardless, which is the
# reliable signal for a native command's real exit code in every
# PowerShell version, unlike $?.
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: 'docker info' failed - the Docker daemon isn't reachable" -ForegroundColor Red
    Write-Host "from this shell/user. On Windows this usually means Docker Desktop"
    Write-Host "isn't running yet, or (WSL2 backend) its integration hasn't started -"
    Write-Host "see README.md's `"Prerequisites`" section."
    exit 1
}

# --- Resolve which services to run -----------------------------------------

$AllServices = @('recommendation', 'web', 'llm', 'catalog')
if ($Services.Count -gt 0) {
    $TargetServices = $Services
}
else {
    $TargetServices = $AllServices
}

$FailedServices = New-Object System.Collections.Generic.List[string]

foreach ($svc in $TargetServices) {
    $svcPath = Join-Path -Path $RepoRoot -ChildPath "services\$svc"
    if (-not (Test-Path -Path $svcPath -PathType Container)) {
        Write-Host "ERROR: services/$svc does not exist - skipping." -ForegroundColor Red
        $FailedServices.Add("$svc (unknown service)")
        continue
    }

    Write-Host ""
    Write-Host "=== $svc ==="

    # Docker Desktop (both the WSL2 and Hyper-V backends) accepts a plain
    # Windows drive-letter host path directly in `-v` and translates it
    # itself - no manual backslash/forward-slash conversion needed. The
    # container-side path (-w) is always a Linux path regardless of host
    # OS, since python:3.11-slim is a Linux image.
    $dockerArgs = @(
        'run', '--rm',
        '-v', "${RepoRoot}:/repo",
        '-w', "/repo/services/$svc",
        '-e', 'TESTING=true',
        'python:3.11-slim',
        'bash', '-c', 'pip install --no-cache-dir -q -r requirements.txt && python -m pytest -q'
    )
    # NOTE: pip's routine "running as root" warning (harmless - the
    # container is disposable) is written to stderr by the container
    # process; Windows PowerShell 5.1 displays any native command's
    # stderr output as a red "NativeCommandError"-style block. This is
    # cosmetic only and does not indicate a failure - the actual
    # pass/fail decision below is $LASTEXITCODE (docker run's real exit
    # code), never affected by what PowerShell prints for stderr content.
    docker @dockerArgs

    if ($LASTEXITCODE -eq 0) {
        Write-Host "=== ${svc}: PASSED ==="
    }
    else {
        Write-Host "=== ${svc}: FAILED ===" -ForegroundColor Red
        $FailedServices.Add($svc)
    }
}

Write-Host ""
if ($FailedServices.Count -eq 0) {
    Write-Host "All service test suites passed."
    exit 0
}
else {
    Write-Host "FAILED service suites: $($FailedServices -join ', ')" -ForegroundColor Red
    exit 1
}
