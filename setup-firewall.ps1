param([ValidateRange(1024,65535)][int]$SipPort = 5060)
$ErrorActionPreference = 'Stop'
$dataPath = if ($env:CODEX_PHONE_DATA) { $env:CODEX_PHONE_DATA } else { Join-Path $PSScriptRoot 'data' }
New-Item -ItemType Directory -Path $dataPath -Force | Out-Null
try {
    foreach ($ruleName in @('Codex Phone SIP', 'Codex Phone RTP')) {
        $existing = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
        if ($existing) { $existing | Remove-NetFirewallRule }
    }
    New-NetFirewallRule -DisplayName 'Codex Phone SIP' -Direction Inbound -Action Allow -Protocol UDP -LocalPort $SipPort -Profile Private -RemoteAddress LocalSubnet | Out-Null
    New-NetFirewallRule -DisplayName 'Codex Phone RTP' -Direction Inbound -Action Allow -Protocol UDP -LocalPort '16384-16500' -Profile Private -RemoteAddress LocalSubnet | Out-Null
    'OK: Local-subnet SIP/RTP enabled for Private networks only.' | Set-Content -LiteralPath (Join-Path $dataPath 'firewall-result.txt')
} catch {
    $_.Exception.Message | Set-Content -LiteralPath (Join-Path $dataPath 'firewall-result.txt')
    exit 1
}
