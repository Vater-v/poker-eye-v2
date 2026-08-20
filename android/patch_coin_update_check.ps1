param(
    [Parameter(Mandatory = $false)]
    [string]$Bundle = (Join-Path $PSScriptRoot "coin\assets\index.android.bundle")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# Coin's custom Hermes AppUpdater stores `!Device.isDebug` as its release-time
# enable flag.  HBC v96 encodes the relevant instruction at this fixed offset as:
#
#   0B 09 07    Not r9, r7
#
# Replacing only the opcode with Mov makes the flag equal Device.isDebug instead.
# It is therefore false in the release APK and checkForUpdate is never called.
# The instruction width and registers stay unchanged, so bytecode offsets, jump
# targets and the Hermes tables are unaffected.
$expectedOriginalSha256 = "BBFAF5A4FD2B6BB412AB81857570CABF11BB0F94516B6CB8A0908ADB6C56219B"
$expectedPatchedSha256 = "2E1B76FCA970C1520FF1DD0F5EA033F29ED4547DD3E92792297A03750AFA40B1"
$opcodeOffset = 0x01EA2633
$original = [byte[]](0x0B, 0x09, 0x07, 0x2C, 0x01, 0x07, 0x09)
$patched  = [byte[]](0x08, 0x09, 0x07, 0x2C, 0x01, 0x07, 0x09)

if (-not (Test-Path -LiteralPath $Bundle -PathType Leaf)) {
    throw "Hermes bundle not found: $Bundle"
}

$item = Get-Item -LiteralPath $Bundle
if ($item.Length -ne 33191308) {
    throw "Unexpected Hermes bundle size $($item.Length); refusing an unsafe patch"
}
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Bundle).Hash

$stream = [IO.File]::Open($Bundle, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $stream.Position = $opcodeOffset
    $actual = New-Object byte[] $original.Length
    if ($stream.Read($actual, 0, $actual.Length) -ne $actual.Length) {
        throw "Cannot read AppUpdater opcode at 0x$($opcodeOffset.ToString('X'))"
    }

    $isOriginal = [Linq.Enumerable]::SequenceEqual($actual, $original)
    $isPatched = [Linq.Enumerable]::SequenceEqual($actual, $patched)
    if ($isPatched) {
        if ($hash -ne $expectedPatchedSha256) {
            throw "Patched opcode is present but Hermes SHA256 is $hash; refusing an unknown bundle"
        }
        Write-Host "Coin custom update check already disabled"
        return
    }
    if (-not $isOriginal) {
        $hex = ($actual | ForEach-Object { $_.ToString('X2') }) -join ' '
        throw "Unexpected AppUpdater bytecode ($hex); refusing an unsafe patch"
    }

    if ($hash -ne $expectedOriginalSha256) {
        throw "Unexpected original Hermes SHA256 $hash; refusing an unsafe patch"
    }

    $stream.Position = $opcodeOffset
    $stream.WriteByte(0x08) # HBC v96 Mov; operands r9,r7 are intentionally retained.
    $stream.Flush($true)
} finally {
    $stream.Dispose()
}

Write-Host "Coin custom release update check disabled"
