# poker-eye-v2 isolated APK build.
# Builds the v2 bridge (UDP discovery + authenticated TCP) into an isolated
# copy of the Coin APK. The baseline tree C:\projects\pokereye\coin is never
# touched; this script operates only on the local coin_v2 copy.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
Set-Location -LiteralPath $root

$apktool     = "C:\projects\pokereye\apktool.jar"
$androidJar  = "C:\Android\platforms\android-35\android.jar"
$d8          = "C:\Android\build-tools\35.0.0\d8.bat"
$zipalign    = "C:\Android\build-tools\35.0.0\zipalign.exe"
$apksigner   = "C:\Android\build-tools\35.0.0\apksigner.bat"
$keystore    = Join-Path $env:USERPROFILE ".android\debug.keystore"
$patchScript = "C:\projects\pokereye\patch_coin_update_check.ps1"
$injectScript = "C:\projects\pokereye\inject_classes_dex.py"
$baselineCoin = "C:\projects\pokereye\coin"

$project   = Join-Path $root "coin_v2"
$javaSrc   = Join-Path $root "HmuriyBridge.java"
$builtinsSource = Join-Path "C:\projects\pokereye\_bridge_src" "kotlin\kotlin"
$serviceSource  = Join-Path "C:\projects\pokereye\_bridge_src" "kotlin\META-INF\services\kotlin.reflect.jvm.internal.impl.builtins.BuiltInsLoader"
$publicSuffixSource = Join-Path "C:\projects\pokereye\_bridge_src" "okhttp3\internal\publicsuffix\publicsuffixes.gz"

$javaTmp  = Join-Path $env:TEMP ("pokereye-v2-bridge-" + $PID)
$classes  = Join-Path $javaTmp "classes"
$dex      = Join-Path $javaTmp "dex"
$dist     = Join-Path $root "out"
$unsigned = Join-Path $dist "coinpoker-v2-unsigned.apk"
$injected = Join-Path $dist "coinpoker-v2-injected.apk"
$aligned  = Join-Path $dist "coinpoker-v2-aligned.apk"
$signed   = Join-Path $dist "coinpoker-v2-debug.apk"

if (-not (Test-Path -LiteralPath $androidJar)) { throw "android.jar missing: $androidJar" }
if (-not (Test-Path -LiteralPath $d8))       { throw "d8 missing: $d8" }
if (-not (Test-Path -LiteralPath (Join-Path $builtinsSource "kotlin.kotlin_builtins"))) { throw "kotlin builtins missing" }

# 1. Fresh isolated copy of the decompiled tree (baseline untouched).
if (Test-Path -LiteralPath $project) { Remove-Item -LiteralPath $project -Recurse -Force }
Copy-Item -Path $baselineCoin -Destination $project -Recurse -Force

# Copy the unknown/ resources the original build script restores.
$builtinsTarget = Join-Path $project "unknown\kotlin"
$serviceTarget  = Join-Path $project "unknown\META-INF\services\kotlin.reflect.jvm.internal.impl.builtins.BuiltInsLoader"
$publicSuffixTarget = Join-Path $project "unknown\okhttp3\internal\publicsuffix\publicsuffixes.gz"
New-Item -ItemType Directory -Force -Path $builtinsTarget, (Split-Path $serviceTarget), (Split-Path $publicSuffixTarget) | Out-Null
Copy-Item -Path (Join-Path $builtinsSource "*") -Destination $builtinsTarget -Recurse -Force
Copy-Item -Force $serviceSource $serviceTarget
Copy-Item -Force $publicSuffixSource $publicSuffixTarget

# Record source hashes before the build (evidence).
$srcHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $javaSrc).Hash
Write-Host "v2 bridge source SHA256: $srcHash"

# 2. Disable the Coin custom update check in the isolated copy.
& $patchScript -Bundle (Join-Path $project "assets\index.android.bundle")
# (PS scripts throw on failure under $ErrorActionPreference="Stop")

# 3. Compile the v2 bridge.
New-Item -ItemType Directory -Force -Path $classes, $dex, $dist | Out-Null
try {
    & javac -source 8 -target 8 -cp $androidJar -d $classes $javaSrc
    if ($LASTEXITCODE -ne 0) { throw "javac failed" }
    $bridgeClasses = @(Get-ChildItem -LiteralPath (Join-Path $classes "com\hmuriy") -File -Filter "HmuriyBridge*.class" | ForEach-Object FullName)
    if ($bridgeClasses.Count -eq 0) { throw "no bridge classes produced" }
    & $d8 --min-api 23 --lib $androidJar --output $dex $bridgeClasses
    if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

    # 4. Rebuild with apktool.
    & java -jar $apktool b $project -o $unsigned -f
    if ($LASTEXITCODE -ne 0) { throw "apktool failed" }

    # 5. Inject classes8.dex.
    & python $injectScript $unsigned (Join-Path $dex "classes.dex") $injected
    if ($LASTEXITCODE -ne 0) { throw "classes8 injection failed" }

    # 6. Align + sign + verify.
    & $zipalign -f -p 4 $injected $aligned
    if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }
    & $apksigner sign --ks $keystore --ks-pass pass:android --key-pass pass:android --out $signed $aligned
    if ($LASTEXITCODE -ne 0) { throw "apksigner failed" }
    & $apksigner verify --verbose $signed
    if ($LASTEXITCODE -ne 0) { throw "APK verification failed" }

    $size = (Get-Item -LiteralPath $signed).Length
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $signed).Hash
    @"
{
  "artifact": "coinpoker-v2-debug.apk",
  "built_from": "isolated copy of C:\\projects\\pokereye\\coin (baseline untouched)",
  "bridge_source_sha256": "$srcHash",
  "size_bytes": $size,
  "sha256": "$hash"
}
"@ | Set-Content -LiteralPath (Join-Path $dist "manifest.json") -Encoding UTF8

    Write-Host ("Build complete: " + $signed) -ForegroundColor Green
    Write-Host ("SHA256: " + $hash)
} finally {
    Remove-Item -LiteralPath $javaTmp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Join-Path $project "build"), (Join-Path $project "original"), (Join-Path $project "unknown") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $unsigned, $injected, $aligned -Force -ErrorAction SilentlyContinue
}