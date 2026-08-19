param(
    [string]$Repo = "C:\projects\pokereye\poker-eye-v2"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

# Be defensive about callers that accidentally pass a quoted path value.
$Repo = $Repo.Trim()
$Repo = $Repo.Trim('"')
if ([string]::IsNullOrWhiteSpace($Repo)) {
    throw "Repo path is empty"
}
$Repo = (Resolve-Path -LiteralPath $Repo).Path
$android = Join-Path $Repo "android"
$distRoot = Join-Path $Repo ".dist"
$baselineCoin = Join-Path $distRoot "baseline\coin"
$apktool = Join-Path $distRoot "tooling\apktool.jar"
$androidJar = "C:\Android\platforms\android-35\android.jar"
$d8 = "C:\Android\build-tools\35.0.0\d8.bat"
$zipalign = "C:\Android\build-tools\35.0.0\zipalign.exe"
$apksigner = "C:\Android\build-tools\35.0.0\apksigner.bat"
$keystore = Join-Path $env:USERPROFILE ".android\debug.keystore"

$javaTemplate = Join-Path $android "HmuriyBridge.java"
$nativeSrc = Join-Path $android "native\HmuriyNative.cpp"
$smaliPatcher = Join-Path $android "patch_realwebsocket_native.py"
$appPatcher = Join-Path $android "patch_mainapplication_native.py"
$updatePatcher = Join-Path $android "patch_coin_update_check.ps1"
$injectDex = Join-Path $android "inject_classes_dex.py"
$secretFile = Join-Path $Repo "secrets\trainer.secret"
$buildIdFile = Join-Path $Repo "BUILD_ID"

foreach ($required in @(
    $baselineCoin, $apktool, $androidJar, $d8, $zipalign, $apksigner,
    $javaTemplate, $nativeSrc, $smaliPatcher, $appPatcher, $updatePatcher, $injectDex, $secretFile, $buildIdFile
)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing build prerequisite: $required"
    }
}

$buildId = (Get-Content -LiteralPath $buildIdFile -Raw).Trim()
if ([string]::IsNullOrWhiteSpace($buildId) -or $buildId -notmatch '^[A-Za-z0-9._-]{1,64}$') {
    throw "Invalid BUILD_ID: '$buildId'"
}
Write-Host "[+] BUILD_ID: $buildId"

$trainerEndpoint = "5.42.124.216:19037"
Write-Host "[+] Trainer endpoint: $trainerEndpoint"
Write-Host "[+] Routing: plain IPv4 via Android default route / SocksDroid"
Write-Host "[+] ADB reverse: disabled/not used"


function Remove-TreeRobust {
    param(
        [Parameter(Mandatory=$true)][string]$Path,
        [int]$Attempts = 6,
        [switch]$AllowStaleRename
    )

    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    $lastError = $null
    for ($attempt = 1; $attempt -le [Math]::Max(1, $Attempts); $attempt++) {
        try {
            # Clear read-only attributes left by apktool/antivirus before removal.
            & cmd.exe /d /c "attrib -R `"$Path\*`" /S /D 2>nul" | Out-Null
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $Path)) { return $null }
        }
        catch {
            $lastError = $_
        }
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
        Start-Sleep -Milliseconds ([Math]::Min(1500, 150 * $attempt))
    }

    if ($AllowStaleRename -and (Test-Path -LiteralPath $Path)) {
        $parent = Split-Path -Parent $Path
        $leaf = Split-Path -Leaf $Path
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
        $stale = Join-Path $parent ($leaf + ".stale-" + $stamp + "-" + $PID)
        try {
            Move-Item -LiteralPath $Path -Destination $stale -Force -ErrorAction Stop
            Write-Warning "Generated workspace was locked; renamed to $stale and continuing with a clean path."
            return $stale
        }
        catch {
            $lastError = $_
        }
    }

    $message = if ($lastError) { $lastError.Exception.Message } else { "unknown lock" }
    throw "Cannot clean generated directory '$Path': $message. Close Explorer/antivirus/processes using .dist\v2workspace and retry. baseline/tooling were not touched."
}

function Find-Ndk {
    # Explicit NDK override.
    foreach ($envName in @("ANDROID_NDK_HOME", "ANDROID_NDK_ROOT")) {
        $candidate = [Environment]::GetEnvironmentVariable($envName)
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            $resolved = (Resolve-Path -LiteralPath $candidate).Path
            if (Test-Path -LiteralPath (Join-Path $resolved "toolchains\llvm\prebuilt")) {
                return $resolved
            }
        }
    }

    # Standard side-by-side Android SDK NDK installs.
    $sdkRoots = @("C:\Android")
    if ($env:ANDROID_SDK_ROOT) { $sdkRoots += $env:ANDROID_SDK_ROOT }
    if ($env:ANDROID_HOME) { $sdkRoots += $env:ANDROID_HOME }
    if ($env:LOCALAPPDATA) { $sdkRoots += (Join-Path $env:LOCALAPPDATA "Android\Sdk") }
    $sdkRoots = @($sdkRoots | Where-Object { $_ } | Select-Object -Unique)

    foreach ($sdkRoot in $sdkRoots) {
        $ndkBase = Join-Path $sdkRoot "ndk"
        if (Test-Path -LiteralPath $ndkBase) {
            $dirs = @(Get-ChildItem -LiteralPath $ndkBase -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
            foreach ($dir in $dirs) {
                if (Test-Path -LiteralPath (Join-Path $dir.FullName "toolchains\llvm\prebuilt")) {
                    return $dir.FullName
                }
            }
        }
    }

    # Unity Hub bundled NDK.
    $unityEditors = "C:\Program Files\Unity\Hub\Editor"
    if (Test-Path -LiteralPath $unityEditors) {
        $unityEditorsByVersion = @(Get-ChildItem -LiteralPath $unityEditors -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)
        foreach ($editor in $unityEditorsByVersion) {
            $candidate = Join-Path $editor.FullName "Editor\Data\PlaybackEngines\AndroidPlayer\NDK"
            if ((Test-Path -LiteralPath $candidate) -and
                (Test-Path -LiteralPath (Join-Path $candidate "toolchains\llvm\prebuilt"))) {
                return (Resolve-Path -LiteralPath $candidate).Path
            }
        }
    }

    # No installed NDK found: locate sdkmanager and install the requested version.
    $sdkmanagerCandidates = @(
        "C:\Android\cmdline-tools\bin\sdkmanager.bat",
        "C:\Android\cmdline-tools\latest\bin\sdkmanager.bat",
        "C:\Android\tools\bin\sdkmanager.bat"
    )
    foreach ($sdkRoot in $sdkRoots) {
        $sdkmanagerCandidates += @(
            (Join-Path $sdkRoot "cmdline-tools\bin\sdkmanager.bat"),
            (Join-Path $sdkRoot "cmdline-tools\latest\bin\sdkmanager.bat"),
            (Join-Path $sdkRoot "tools\bin\sdkmanager.bat")
        )
    }

    $sdkmanager = $sdkmanagerCandidates |
        Where-Object { Test-Path -LiteralPath $_ } |
        Select-Object -First 1

    if (-not $sdkmanager) {
        throw "Android NDK is missing and sdkmanager.bat was not found. Set ANDROID_NDK_HOME or install an Android NDK."
    }

    $installSdkRoot = if ($env:ANDROID_SDK_ROOT) {
        $env:ANDROID_SDK_ROOT
    } elseif ($env:ANDROID_HOME) {
        $env:ANDROID_HOME
    } else {
        "C:\Android"
    }

    $ndkBase = Join-Path $installSdkRoot "ndk"
    $version = if ($env:POKEREYE_NDK_VERSION) { $env:POKEREYE_NDK_VERSION } else { "27.2.12479018" }

    Write-Host "[*] Android NDK not found; installing ndk;$version ..."
    & $sdkmanager "ndk;$version"
    if ($LASTEXITCODE -ne 0) {
        throw "sdkmanager failed to install ndk;$version"
    }

    $installed = Join-Path $ndkBase $version
    if (-not (Test-Path -LiteralPath $installed)) {
        throw "NDK install completed but directory not found: $installed"
    }
    return $installed
}

$ndk = Find-Ndk
Write-Host "[+] NDK: $ndk"

$prebuilt = Get-ChildItem -LiteralPath (Join-Path $ndk "toolchains\llvm\prebuilt") -Directory | Select-Object -First 1
if (-not $prebuilt) { throw "NDK LLVM prebuilt directory missing" }
$bin = Join-Path $prebuilt.FullName "bin"

$clang = Join-Path $bin "clang++.exe"
if (-not (Test-Path -LiteralPath $clang)) {
    throw "NDK clang++ missing: $clang"
}

# Invoke clang.exe directly with --target instead of the Windows forwarding
# wrapper scripts. More importantly for Windows PowerShell 5.1, every argument
# is passed through an explicit string array so commas in -Wl,... are never
# interpreted by the PowerShell parser.
$abiTargets = [ordered]@{
    "arm64-v8a"   = "aarch64-linux-android23"
    "armeabi-v7a" = "armv7a-linux-androideabi23"
    "x86_64"      = "x86_64-linux-android23"
    "x86"         = "i686-linux-android23"
}

$work = Join-Path $env:TEMP ("pokereye-native-" + $PID)
$classes = Join-Path $work "classes"
$dexOut = Join-Path $work "dex"
$nativeOut = Join-Path $work "native"
$workspaceRoot = Join-Path $distRoot "v2workspace"
$workspace = Join-Path $workspaceRoot "coin_production_native"
$outDir = Join-Path $workspaceRoot "out"
$unsigned = Join-Path $outDir "coinpoker-production-native-unsigned.apk"
$injected = Join-Path $outDir "coinpoker-production-native-injected.apk"
$aligned = Join-Path $outDir "coinpoker-production-native-aligned.apk"
$signed = Join-Path $outDir "coinpoker-production-native-debug.apk"

Remove-TreeRobust -Path $work -Attempts 4 -AllowStaleRename | Out-Null
# v2workspace is generated output only. Purge it completely so an old APK from
# coin_v2/out can never be mistaken for the native build. Keep baseline/tooling.
Remove-TreeRobust -Path $workspaceRoot -Attempts 6 -AllowStaleRename | Out-Null
New-Item -ItemType Directory -Force -Path $classes, $dexOut, $nativeOut, $outDir | Out-Null

# Do not pass a quoted C string through -D on Windows PowerShell 5.1: the native
# argv layer strips those quotes and clang sees V7.2.1-HMN1-LOCAL as C++ tokens.
# A forced-include header preserves the value exactly as a string literal.
$buildIdHeader = Join-Path $work "pokereye_build_id.h"
$buildIdHeaderText = "#pragma once`n#define POKEREYE_BUILD_ID `"$buildId`"`n"
[IO.File]::WriteAllText($buildIdHeader, $buildIdHeaderText, (New-Object Text.UTF8Encoding($false)))

try {
    # 1) Build libhmuriy.so for all practical APK ABIs.
    $nativeHashes = @{}
    foreach ($entry in $abiTargets.GetEnumerator()) {
        $abi = [string]$entry.Key
        $targetTriple = [string]$entry.Value

        $abiOut = Join-Path $nativeOut $abi
        New-Item -ItemType Directory -Force -Path $abiOut | Out-Null
        $so = Join-Path $abiOut "libhmuriy.so"

        Write-Host "[*] C++ ${abi} -> libhmuriy.so target=$targetTriple"

        $clangArgs = @(
            "--target=$targetTriple",
            "-std=c++17",
            "-O2",
            "-DNDEBUG",
            "-include",
            $buildIdHeader,
            "-fPIC",
            "-fvisibility=hidden",
            "-ffunction-sections",
            "-fdata-sections",
            "-pthread",
            "-static-libstdc++",
            "-shared",
            "-Wl,--gc-sections",
            "-Wl,-z,relro,-z,now",
            "-Wl,--build-id=sha1",
            "-o",
            $so,
            $nativeSrc,
            "-llog",
            "-landroid",
            "-latomic"
        )

        & $clang @clangArgs
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $so)) {
            throw "native compile failed for ${abi} (target=$targetTriple)"
        }
        $nativeHashes[$abi] = (Get-FileHash -Algorithm SHA256 -LiteralPath $so).Hash
    }

    # 2) Compile the tiny Java JNI adapter with the runtime secret injected.
    $secret = (Get-Content -LiteralPath $secretFile -Raw).Trim()
    if ([string]::IsNullOrWhiteSpace($secret)) { throw "trainer.secret is empty" }

    $javaSrc = Join-Path $work "HmuriyBridge.java"
    $javaText = Get-Content -LiteralPath $javaTemplate -Raw
    $javaText = $javaText.Replace('__POKEREYE_V2_SECRET__', $secret.Replace('"','\"'))
    [IO.File]::WriteAllText($javaSrc, $javaText, (New-Object Text.UTF8Encoding($false)))

    $javacArgs = @("-source", "8", "-target", "8", "-cp", $androidJar, "-d", $classes, $javaSrc)
    & javac @javacArgs
    if ($LASTEXITCODE -ne 0) { throw "javac failed" }

    $bridgeClasses = @(
        Get-ChildItem -LiteralPath (Join-Path $classes "com\hmuriy") -File -Filter "HmuriyBridge*.class" |
        ForEach-Object FullName
    )
    if ($bridgeClasses.Count -eq 0) { throw "HmuriyBridge class missing after javac" }

    $d8Args = @("--min-api", "23", "--lib", $androidJar, "--output", $dexOut)
    $d8Args += $bridgeClasses
    & $d8 @d8Args
    if ($LASTEXITCODE -ne 0) { throw "d8 failed" }

    # 3) Fresh isolated copy of baseline APK tree.
    Copy-Item -Path $baselineCoin -Destination $workspace -Recurse -Force

    $wsSmali = Join-Path $workspace "smali_classes6\okhttp3\internal\ws\RealWebSocket.smali"
    if (-not (Test-Path -LiteralPath $wsSmali)) {
        throw "RealWebSocket.smali not found in baseline"
    }

    & python $smaliPatcher $wsSmali
    if ($LASTEXITCODE -ne 0) { throw "RealWebSocket native patch failed" }

    $appSmali = Join-Path $workspace "smali_classes2\com\coingames\coinpoker\MainApplication.smali"
    if (-not (Test-Path -LiteralPath $appSmali)) {
        throw "MainApplication.smali not found in baseline"
    }
    & python $appPatcher $appSmali
    if ($LASTEXITCODE -ne 0) { throw "MainApplication native bootstrap patch failed" }

    # Disable the copied HTTP payload logger entirely.
    $httpLoggerSmali = Join-Path $workspace "smali_classes7\com\hmuriy\HmuriyLogger.smali"
    if (Test-Path -LiteralPath $httpLoggerSmali) {
        $httpText = Get-Content -LiteralPath $httpLoggerSmali -Raw
        $stub = '$1' + [Environment]::NewLine + '    .locals 0' + [Environment]::NewLine +
                '    return-void' + [Environment]::NewLine + '$2'
        $httpText = $httpText -replace '(?s)(\.method public log\(Ljava/lang/String;\)V).*?(\.end method)', $stub
        [IO.File]::WriteAllText($httpLoggerSmali, $httpText, (New-Object Text.UTF8Encoding($false)))
    }

    # Restore resources apktool needs from the known tooling cache.
    $builtinsSource = Join-Path $distRoot "tooling\kotlin\kotlin"
    $serviceSource = Join-Path $distRoot "tooling\kotlin\META-INF\services\kotlin.reflect.jvm.internal.impl.builtins.BuiltInsLoader"
    $publicSuffixSource = Join-Path $distRoot "tooling\okhttp3\internal\publicsuffix\publicsuffixes.gz"

    $builtinsTarget = Join-Path $workspace "unknown\kotlin"
    $serviceTarget = Join-Path $workspace "unknown\META-INF\services\kotlin.reflect.jvm.internal.impl.builtins.BuiltInsLoader"
    $publicSuffixTarget = Join-Path $workspace "unknown\okhttp3\internal\publicsuffix\publicsuffixes.gz"

    New-Item -ItemType Directory -Force -Path $builtinsTarget, (Split-Path $serviceTarget), (Split-Path $publicSuffixTarget) | Out-Null
    Copy-Item -Path (Join-Path $builtinsSource "*") -Destination $builtinsTarget -Recurse -Force
    Copy-Item -Force $serviceSource $serviceTarget
    Copy-Item -Force $publicSuffixSource $publicSuffixTarget

    # Add native libs to the normal APK lib/<abi>/ layout.
    foreach ($abi in $abiTargets.Keys) {
        $target = Join-Path $workspace ("lib\" + $abi)
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        Copy-Item -Force (Join-Path $nativeOut ($abi + "\libhmuriy.so")) (Join-Path $target "libhmuriy.so")
    }

    # Keep the existing Coin update-check patch.
    & $updatePatcher -Bundle (Join-Path $workspace "assets\index.android.bundle")
    if ($LASTEXITCODE -ne 0) { throw "Coin update-check patch failed" }

    # 4) Build, inject HmuriyBridge dex, align/sign.
    $apktoolArgs = @("-jar", $apktool, "b", $workspace, "-o", $unsigned, "-f")
    & java @apktoolArgs
    if ($LASTEXITCODE -ne 0) { throw "apktool build failed" }

    $injectArgs = @($injectDex, $unsigned, (Join-Path $dexOut "classes.dex"), $injected)
    & python @injectArgs
    if ($LASTEXITCODE -ne 0) { throw "classes.dex injection failed" }

    $zipalignArgs = @("-f", "-p", "4", $injected, $aligned)
    & $zipalign @zipalignArgs
    if ($LASTEXITCODE -ne 0) { throw "zipalign failed" }

    $signArgs = @(
        "sign",
        "--ks", $keystore,
        "--ks-pass", "pass:android",
        "--key-pass", "pass:android",
        "--out", $signed,
        $aligned
    )
    & $apksigner @signArgs
    if ($LASTEXITCODE -ne 0) { throw "apksigner sign failed" }

    $verifyArgs = @("verify", "--verbose", $signed)
    & $apksigner @verifyArgs
    if ($LASTEXITCODE -ne 0) { throw "APK signature verification failed" }

    $manifest = [ordered]@{
        artifact = "coinpoker-production-native-debug.apk"
        build_id = $buildId
        bridge = "libhmuriy.so C++17 + zero-copy ByteString JNI tap"
        native_source_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $nativeSrc).Hash
        bridge_source_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $javaTemplate).Hash
        abis = $nativeHashes
        size_bytes = (Get-Item -LiteralPath $signed).Length
        sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $signed).Hash
    }
    $manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $outDir "production-native-manifest.json") -Encoding UTF8

    Write-Host ""
    Write-Host "[+] Native APK build complete [$buildId]:" -ForegroundColor Green
    Write-Host "    $signed"
    Write-Host ("    SHA256 " + $manifest.sha256)
}
finally {
    try { Remove-TreeRobust -Path $work -Attempts 3 -AllowStaleRename | Out-Null } catch { Write-Warning $_.Exception.Message }
    Remove-Item -LiteralPath $unsigned, $injected, $aligned -Force -ErrorAction SilentlyContinue
    foreach ($generated in @(
        (Join-Path $workspace "build"),
        (Join-Path $workspace "original"),
        (Join-Path $workspace "unknown")
    )) {
        try { Remove-TreeRobust -Path $generated -Attempts 2 | Out-Null } catch { Write-Warning $_.Exception.Message }
    }
}
