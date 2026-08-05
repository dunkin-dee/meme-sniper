<#
.SYNOPSIS
    Push the application code to the EC2 host and run bootstrap.sh.

.DESCRIPTION
    Never copy the project directory wholesale. Two things in it must not
    travel:

      data/   - the live SQLite database. Copied while the recorder is writing
                it, you get a torn file with a detached WAL, and on the remote
                it would shadow whatever the instance had already collected.
      .venv/  - a Windows virtualenv. Useless on Linux, and it collides with
                the venv bootstrap.sh builds.

    So this packs a tarball with those excluded, unpacks it to a staging dir on
    the remote, and hands the staging dir to bootstrap.sh as REPO_SRC.
    bootstrap.sh then rsyncs with --delete, which prunes files deleted locally
    while still leaving the instance's data/ untouched.

    Safe to re-run: this is also the deploy path for code updates.

.EXAMPLE
    .\deploy\push-code.ps1 -RemoteHost ec2-1-2-3-4.compute.amazonaws.com -KeyFile ~\.ssh\sniper.pem
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RemoteHost,
    [string]$User = "ubuntu",
    [string]$KeyFile,
    # Upload only; skip provisioning. Useful to stage code before a cutover.
    [switch]$NoBootstrap
)

$ErrorActionPreference = "Stop"

$sshArgs = @()
if ($KeyFile) { $sshArgs += @("-i", $KeyFile) }
$target = "$User@$RemoteHost"

if (-not (Test-Path "pyproject.toml")) {
    throw "run this from the project root (no pyproject.toml here)"
}

# Build outside the project tree so the archive can never contain itself.
$tarball = Join-Path ([System.IO.Path]::GetTempPath()) "meme-sniper-push.tgz"

Write-Host "==> Packing source (excluding data/ and .venv/)" -ForegroundColor Cyan
# Windows 11 ships bsdtar as tar.exe; -czf with --exclude is supported.
& tar --exclude=.venv --exclude=data --exclude=.git --exclude=__pycache__ `
      --exclude=.pytest_cache --exclude='*.egg-info' --exclude='*.db*' `
      -czf $tarball .
if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE)" }
Write-Host "    $([math]::Round((Get-Item $tarball).Length / 1KB)) KB" -ForegroundColor DarkGray

Write-Host "==> Uploading to $RemoteHost" -ForegroundColor Cyan
& scp @sshArgs $tarball "${target}:/tmp/meme-sniper-push.tgz"
if ($LASTEXITCODE -ne 0) { throw "scp failed (exit $LASTEXITCODE)" }

Write-Host "==> Unpacking to staging dir" -ForegroundColor Cyan
$unpack = "rm -rf /tmp/meme-sniper-src && mkdir -p /tmp/meme-sniper-src && " +
          "tar -xzf /tmp/meme-sniper-push.tgz -C /tmp/meme-sniper-src && " +
          "rm -f /tmp/meme-sniper-push.tgz && ls /tmp/meme-sniper-src"
& ssh @sshArgs $target $unpack
if ($LASTEXITCODE -ne 0) { throw "remote unpack failed (exit $LASTEXITCODE)" }

Remove-Item $tarball -Force

if ($NoBootstrap) {
    Write-Host "==> Staged at /tmp/meme-sniper-src (bootstrap skipped)" -ForegroundColor Yellow
    Write-Host "    sudo REPO_SRC=/tmp/meme-sniper-src bash /tmp/meme-sniper-src/deploy/bootstrap.sh"
    exit 0
}

Write-Host "==> Running bootstrap.sh (idempotent)" -ForegroundColor Cyan
& ssh @sshArgs $target "sudo REPO_SRC=/tmp/meme-sniper-src bash /tmp/meme-sniper-src/deploy/bootstrap.sh"
if ($LASTEXITCODE -ne 0) { throw "bootstrap failed (exit $LASTEXITCODE)" }

Write-Host "==> Done. Next: configure Litestream, then run ratecheck after ~1h." -ForegroundColor Green
