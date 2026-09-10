$ErrorActionPreference = 'Stop'
$destination = Join-Path $PSScriptRoot '..\build\windows\MicrosoftEdgeWebview2Setup.exe'
New-Item -ItemType Directory -Force (Split-Path $destination) | Out-Null
Invoke-WebRequest -Uri 'https://go.microsoft.com/fwlink/p/?LinkId=2124703' -OutFile $destination
$signature = Get-AuthenticodeSignature -LiteralPath $destination
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') {
    Remove-Item -LiteralPath $destination
    throw 'WebView2 bootstrapper must have a valid Microsoft signature'
}
Write-Output "Verified WebView2 bootstrapper: $destination"
