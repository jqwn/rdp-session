param(
    [Parameter(Mandatory = $true)]
    [string]$Username,

    [string]$Domain,
    [string]$ToolPath = "C:\Tools\rdp-session.exe",
    [string]$HostName = "127.0.0.1",
    [string]$PasswordEnv = "RDP_PASSWORD",
    [switch]$PasswordStdin,
    [switch]$AllowInsecureCert,
    [string]$ScreenshotPath,
    [string]$DismissAction = "none"
)

$toolArgs = @(
    "--output", "json",
    "create",
    "--host", $HostName,
    "--username", $Username,
    "--dismiss-action", $DismissAction
)

if ($PasswordStdin) {
    $toolArgs += "--password-stdin"
} else {
    if (-not [Environment]::GetEnvironmentVariable($PasswordEnv)) {
        throw "$PasswordEnv is not set"
    }
    $toolArgs += @("--password-env", $PasswordEnv)
}

if ($AllowInsecureCert) {
    $toolArgs += "--allow-insecure-cert"
}

if ($Domain) {
    $toolArgs += @("--domain", $Domain)
}

if ($ScreenshotPath) {
    $toolArgs += @("--screenshot", $ScreenshotPath)
}

if ($PasswordStdin) {
    [Console]::In.ReadLine() | & $ToolPath @toolArgs
} else {
    & $ToolPath @toolArgs
}
exit $LASTEXITCODE
