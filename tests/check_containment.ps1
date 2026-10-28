# Trusted host orchestration only; every probe executes inside a container.
$ErrorActionPreference = 'Stop'
$probeRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('harness-probe-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $probeRoot | Out-Null

function Invoke-Probe([string]$command, [string]$deadline) {
    $probeName = 'harness-probe-' + [guid]::NewGuid().ToString('N')
    try {
        docker create --name $probeName --pull=never --network=none --read-only --cap-drop=ALL --security-opt=no-new-privileges --pids-limit=128 --memory=512m --cpus=1 --user=65534:65534 --mount "type=bind,source=$probeRoot,target=/workspace" --tmpfs '/tmp:rw,nosuid,nodev,size=64m,mode=1777' --workdir=/workspace --env=HOME=/tmp --env=PATH=/usr/local/bin:/usr/bin:/bin --env=LANG=C.UTF-8 --env=PYTHONDONTWRITEBYTECODE=1 --entrypoint=/usr/bin/timeout coding-harness-sandbox:local --signal=KILL $deadline /bin/bash --noprofile --norc -c $command | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'container creation failed' }
        docker start --attach $probeName
        $probeExit = docker inspect --format '{{.State.ExitCode}}' $probeName
        if ($LASTEXITCODE -ne 0) { throw 'container inspection failed' }
        return [int]$probeExit
    } finally {
        docker rm --force $probeName | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "container cleanup failed: $probeName" }
    }
}

try {
    $isolation = @'
test "$HOME" = /tmp || exit 1
test ! -e /var/run/docker.sock || exit 2
test ! -e /workspace/.git || exit 3
test -z "$HOST_SECRET_SENTINEL" || exit 4
if touch /host-write-probe 2>/dev/null; then exit 5; fi
if command -v git >/dev/null; then exit 6; fi
/usr/local/bin/python - <<'PY'
import socket
s = socket.socket()
s.settimeout(1)
assert s.connect_ex(('1.1.1.1', 443)) != 0
PY
'@
    $env:HOST_SECRET_SENTINEL = 'must-not-enter-container'
    if ((Invoke-Probe $isolation '5') -ne 0) { throw 'isolation probe failed' }
    Remove-Item Env:HOST_SECRET_SENTINEL

    # setsid detaches the writer from its parent's process group.
    $writer = @'
/usr/local/bin/python - <<'PY' &
import os, time
os.setsid()
with open('heartbeat', 'a') as f:
    for _ in range(1000):
        f.write('x')
        f.flush()
        time.sleep(.01)
PY
sleep .2
'@
    if ((Invoke-Probe $writer '5') -ne 0) { throw 'normal-exit probe failed' }
    $heartbeat = Join-Path $probeRoot 'heartbeat'
    $afterExit = (Get-Item -LiteralPath $heartbeat).Length
    Start-Sleep -Milliseconds 250
    if ((Get-Item -LiteralPath $heartbeat).Length -ne $afterExit) { throw 'detached child survived normal exit' }

    $stuck = $writer + "`nsleep 30"
    if ((Invoke-Probe $stuck '1') -ne 137) { throw 'deadline did not kill container' }
    $afterDeadline = (Get-Item -LiteralPath $heartbeat).Length
    Start-Sleep -Milliseconds 250
    if ((Get-Item -LiteralPath $heartbeat).Length -ne $afterDeadline) { throw 'writes continued after deadline' }
    Write-Output 'PASS: host isolation, detached child cleanup, deadline, and no subsequent writes'
} finally {
    # Resolve and validate the exact temporary directory before recursive removal.
    $resolvedProbe = [System.IO.Path]::GetFullPath($probeRoot)
    $resolvedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if (-not $resolvedProbe.StartsWith($resolvedTemp, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not ([System.IO.Path]::GetFileName($resolvedProbe)).StartsWith('harness-probe-')) {
        throw 'unsafe probe cleanup path'
    }
    Remove-Item -LiteralPath $resolvedProbe -Recurse -Force
}
