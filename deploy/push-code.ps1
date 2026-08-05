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

# bootstrap.sh runs apt-get with -qq, which can emit nothing for ~20 minutes on
# a t3.micro. A silent SSH channel gets dropped by NAT/idle timeouts; when that
# happened the remote bootstrap took SIGHUP and died while this script sat on a
# half-open socket for another 48 minutes without noticing (observed
# 2026-08-05). Keepalives make the channel non-idle AND make a genuine drop
# fail fast instead of hanging.
$sshArgs = @("-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=6")
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

# Run bootstrap DETACHED (setsid, output to a log, exit code to a sentinel)
# rather than as a foreground SSH command. A dropped connection then costs us
# only the log tail - the provisioning keeps running on the instance and we
# reattach on the next poll. Tying a 20-minute install to the lifetime of one
# TCP connection is what broke this before.
Write-Host "==> Running bootstrap.sh (detached, idempotent)" -ForegroundColor Cyan

$launch = @'
sudo rm -f /tmp/bootstrap.log /tmp/bootstrap.done
sudo setsid bash -c 'REPO_SRC=/tmp/meme-sniper-src bash /tmp/meme-sniper-src/deploy/bootstrap.sh >/tmp/bootstrap.log 2>&1; echo $? >/tmp/bootstrap.done' </dev/null >/dev/null 2>&1 &
sleep 1; echo detached
'@
& ssh @sshArgs $target $launch
if ($LASTEXITCODE -ne 0) { throw "could not launch bootstrap (exit $LASTEXITCODE)" }

$shown = 0
$deadline = (Get-Date).AddMinutes(45)
$exitCode = $null

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 10

    # A failed poll is transient (connection blip); keep waiting rather than
    # aborting a provision that is still running perfectly well remotely.
    #
    # PowerShell 5.1 wraps a native command's stderr in a NativeCommandError,
    # which $ErrorActionPreference='Stop' promotes to TERMINATING - so an
    # ordinary "connection timed out" killed this loop outright instead of
    # retrying. Drop to Continue for the call and test $LASTEXITCODE by hand.
    $poll = $null
    try {
        $ErrorActionPreference = "Continue"
        $poll = & ssh @sshArgs $target `
            "tail -n +$($shown + 1) /tmp/bootstrap.log 2>/dev/null; echo '::MARK::'; cat /tmp/bootstrap.done 2>/dev/null"
    } catch {
        $poll = $null
    } finally {
        $ErrorActionPreference = "Stop"
    }
    if ($null -eq $poll -or $LASTEXITCODE -ne 0) {
        Write-Host "    (poll failed, retrying)" -ForegroundColor DarkGray
        continue
    }

    $lines = @($poll)
    $mark = [array]::IndexOf($lines, "::MARK::")
    if ($mark -lt 0) { continue }

    if ($mark -gt 0) {
        $lines[0..($mark - 1)] | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        $shown += $mark
    }

    if ($lines.Count -gt $mark + 1 -and $lines[$mark + 1] -match '^\d+$') {
        $exitCode = [int]$lines[$mark + 1]
        break
    }
}

if ($null -eq $exitCode) { throw "bootstrap did not finish within 45 min - ssh in and check /tmp/bootstrap.log" }
if ($exitCode -ne 0)     { throw "bootstrap failed (exit $exitCode) - see /tmp/bootstrap.log on the instance" }

Write-Host "==> Done. Next: configure Litestream, then run ratecheck after ~1h." -ForegroundColor Green
