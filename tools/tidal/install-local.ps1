param([switch]$ImportOctetSession, [switch]$SkipDependencies)
$ErrorActionPreference = 'Stop'
$tidalRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$tidalPython = Join-Path $tidalRoot '.venv-tidal\Scripts\python.exe'
if (-not $SkipDependencies) {
    if (-not (Test-Path -LiteralPath $tidalPython)) {
        python -m venv (Join-Path $tidalRoot '.venv-tidal')
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the Tidal environment.' }
    }
    & $tidalPython -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Could not install Tidal dependencies.' }
}
$tidalState = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'OctetTidal'
New-Item -ItemType Directory -Path $tidalState -Force | Out-Null
# Restrict the dedicated private directory before any credential is written.
$tidalSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$tidalAcl = [Security.AccessControl.DirectorySecurity]::new()
$tidalAcl.SetOwner($tidalSid)
$tidalAcl.SetAccessRuleProtection($true, $false)
$tidalRule = [Security.AccessControl.FileSystemAccessRule]::new(
    $tidalSid, [Security.AccessControl.FileSystemRights]::FullControl,
    ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
    [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
$tidalAcl.AddAccessRule($tidalRule)
Set-Acl -LiteralPath $tidalState -AclObject $tidalAcl
$tidalSessionPath = Join-Path $tidalState 'session.json'
if ($ImportOctetSession) {
    if (Test-Path -LiteralPath $tidalSessionPath) { throw 'A local session already exists; leaving it intact.' }
    # Capture the secret in memory and write directly into the private directory.
    # Never echo, log, or copy it into the repository.
    $tidalSecret = ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o HostKeyAlias=octet -o ConnectTimeout=10 octet@100.100.177.81 'sudo -n -u upmpdcli cat /var/lib/octet-remote/tidal-session.json'
    if ($LASTEXITCODE -ne 0) { throw 'Could not import the Tidal session from Octet.' }
    [IO.File]::WriteAllText($tidalSessionPath, ($tidalSecret -join [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
    $tidalSecret = $null
}
Write-Output "Local Tidal CLI: $(Join-Path $PSScriptRoot 'tidal.cmd')"
Write-Output "Private state: $tidalState"
Write-Output 'Installation ready. Run tidal.cmd status, or tidal.cmd login for a new independent authorization.'
