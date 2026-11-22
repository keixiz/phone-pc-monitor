# 关闭 Windows 移动热点（WinRT 的 NetworkOperatorTetheringManager）
# 不需要管理员；热点本来没开就只提示一句，不会动其他网络设置

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Host '[错误] 需要 Windows 10/11 自带的 PowerShell 5.1，当前版本太旧。'
    Write-Host '       请手动关闭热点：设置 > 网络和 Internet > 移动热点（把开关关掉）'
    exit 1
}

$ErrorActionPreference = 'Stop'

function Show-ManualHint {
    param([string]$Reason)
    if ($Reason) { Write-Host "  原因：$Reason" }
    Write-Host '  手动关闭：设置 > 网络和 Internet > 移动热点（把开关关掉）'
    Write-Host '  Manual:     Settings > Network & Internet > Mobile hotspot'
    Write-Host ''
}

# ---------------- 1. 加载 WinRT 组件 ----------------

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
    $null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
    $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
    $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
} catch {
    Write-Host '[错误] 这台电脑的系统组件里没有 WinRT 网络共享接口，无法自动关热点。'
    Show-ManualHint '系统版本过低或组件被精简'
    exit 1
}

$asTaskOperation = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and
        $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

if ($null -eq $asTaskOperation) {
    Write-Host '[错误] 系统运行库异常：找不到 WinRT 异步调用接口。'
    Show-ManualHint '可能是 .NET 组件损坏，重启电脑后再试'
    exit 1
}

function Wait-WinRtOperation {
    param($Operation, [Type]$ResultType)
    $asTask = $asTaskOperation.MakeGenericMethod($ResultType)
    $task = $asTask.Invoke($null, @($Operation))
    if (-not $task.Wait(120000)) {
        throw 'WinRT 调用 120 秒未返回，系统热点功能可能卡住了'
    }
    return $task.Result
}

# ---------------- 2. 收集可用的网络配置 ----------------

$profiles = New-Object System.Collections.ArrayList

try {
    $internet = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    if ($null -ne $internet) { [void]$profiles.Add($internet) }
} catch { }

try {
    foreach ($cp in [Windows.Networking.Connectivity.NetworkInformation]::GetConnectionProfiles()) {
        $dup = $false
        foreach ($known in $profiles) {
            if ($known.ProfileName -eq $cp.ProfileName) { $dup = $true; break }
        }
        if (-not $dup) { [void]$profiles.Add($cp) }
    }
} catch { }

if ($profiles.Count -eq 0) {
    Write-Host '[提示] 检测不到任何网络连接，无法判断热点状态。'
    Write-Host '       如果热点其实还开着，请手动关掉：设置 > 网络和 Internet > 移动热点'
    Write-Host ''
    exit 1
}

# ---------------- 3. 找到正在运行的热点并关闭 ----------------

$stopped = $false
$foundOn = $false

foreach ($p in $profiles) {
    $manager = $null
    try {
        $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($p)
    } catch {
        continue
    }
    if ($null -eq $manager) { continue }
    if ([string]$manager.TetheringOperationalState -ne 'On') { continue }

    $foundOn = $true
    Write-Host "正在关闭热点（网络：$($p.ProfileName)）…"

    $result = $null
    try {
        $resultType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
        $result = Wait-WinRtOperation ($manager.StopTetheringAsync()) $resultType
    } catch {
        Write-Host '[错误] 调用关热点接口时出错。'
        Show-ManualHint $_.Exception.Message
        exit 2
    }

    if ($null -ne $result -and [string]$result.Status -ne 'Success') {
        $msg = [string]$result.Status
        if ($result.AdditionalErrorMessage) { $msg = $msg + '：' + [string]$result.AdditionalErrorMessage }
        Write-Host "[错误] 热点没能关掉（$msg）。"
        Show-ManualHint $null
        exit 3
    }

    # 等状态真正变成 Off
    for ($i = 0; $i -lt 10; $i++) {
        if ([string]$manager.TetheringOperationalState -eq 'Off') { break }
        Start-Sleep -Milliseconds 300
    }

    $stopped = $true
    break
}

if (-not $foundOn) {
    Write-Host '[提示] 热点未开启，不需要关闭。'
    Write-Host ''
    exit 0
}

if ($stopped) {
    Write-Host '[完成] 热点已关闭，手机就连不上这台电脑了。'
}
Write-Host ''
exit 0
