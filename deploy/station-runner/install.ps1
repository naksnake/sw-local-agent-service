# Install the SW Local Agent Service station runner on a Windows test station, offline.
#
#   Expand-Archive or tar -xzf slas-station-runner-<version>.tgz; cd slas-station-runner-<version>
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# What happens: a virtual environment under %LOCALAPPDATA%\slas-station-runner\venv is filled
# from the wheels in this bundle (no network), the platform CA is copied next to it, and a
# scheduled task starts the runner at the operator's logon. The station still has no identity
# afterwards: the next step is `slas-station-runner enrol` with the one-time code from
# Admin → Stations, then `doctor`.
#
# GUI steps on Windows: the runner's Windows GUI backend needs PyAutoGUI, which is not an
# approved dependency yet (CLAUDE.md §0.3). Until it is, the runner on Windows performs
# commands, state and control batches and refuses GUI steps with a sentence; `doctor` says so.
# The operator can still watch and take over through VNC when a TightVNC service listens on
# the configured port.
$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$state = if ($env:SLAS_RUNNER_STATE) { $env:SLAS_RUNNER_STATE } else { Join-Path $env:LOCALAPPDATA "slas-station-runner" }
$venv = Join-Path $state "venv"

$python = Get-Command "py" -ErrorAction SilentlyContinue
if ($python) {
    $pythonCmd = @("py", "-3.12")
} else {
    $python = Get-Command "python" -ErrorAction SilentlyContinue
    $pythonCmd = @("python")
}
if (-not $python) {
    Write-Output "Python 3.12 is not installed on this station."
    Write-Output "The runner needs it; install the official 64-bit Python 3.12 from offline media (with the py launcher) and run this again."
    exit 1
}
$versionOk = & $pythonCmd[0] $pythonCmd[1..($pythonCmd.Length - 1)] -c "import sys; print(sys.version_info[:2] == (3, 12))"
if ($versionOk -ne "True") {
    Write-Output "The Python found is not 3.12; the runner needs 3.12. Install it and run this again."
    exit 1
}

New-Item -ItemType Directory -Force -Path $state | Out-Null
Write-Output "Creating the virtual environment under $venv."
& $pythonCmd[0] $pythonCmd[1..($pythonCmd.Length - 1)] -m venv --clear $venv
$pip = Join-Path $venv "Scripts\python.exe"
& $pip -m pip install --quiet --no-index --find-links (Join-Path $here "wheels") slas-station-runner
Copy-Item (Join-Path $here "slas-ca.pem") (Join-Path $state "slas-ca.pem") -Force

# Private files: only this user may read the state directory.
$acl = Get-Acl $state
$acl.SetAccessRuleProtection($true, $false)
$rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().Name, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow")
$acl.SetAccessRule($rule)
Set-Acl $state $acl

$exe = Join-Path $venv "Scripts\slas-station-runner.exe"
$action = New-ScheduledTaskAction -Execute $exe -Argument "--state-dir `"$state`" serve"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Days 365)
try {
    Register-ScheduledTask -TaskName "SLAS Station Runner" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
    Write-Output "The scheduled task 'SLAS Station Runner' starts the runner at your logon, once the station is enrolled."
} catch {
    Write-Output "The scheduled task could not be registered ($($_.Exception.Message)). Start the runner by hand after enrolment: $exe serve"
}

Write-Output ""
Write-Output "Installed under $venv. Next, on this station, with the code from Admin -> Stations:"
Write-Output "  $exe enrol --platform https://<factory-executor>:8444 --station <name> --code XXXX-XXXX-XXXX --runner-url https://<this station>:8443 --ca $state\slas-ca.pem"
Write-Output "  $exe doctor"
Write-Output "  Start-ScheduledTask 'SLAS Station Runner'   # or: $exe serve"
