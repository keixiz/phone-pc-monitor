# 开启 Windows 移动热点（WinRT 的 NetworkOperatorTetheringManager），并打印手机访问地址
# 只用当前用户权限，不需要管理员；失败时给中文原因 + 手动操作路径，不会静默退出

if ($PSVersionTable.PSVersion.Major -lt 5) {
    Write-Host '[错误] 需要 Windows 10/11 自带的 PowerShell 5.1，当前版本太旧。'
    Write-Host '       请手动开启热点：设置 > 网络和 Internet > 移动热点'
    exit 1
}

$ErrorActionPreference = 'Stop'

# ---------------- 通用小工具 ----------------

function Show-ManualHint {
    param([string]$Reason)
    if ($Reason) { Write-Host "  原因：$Reason" }
    Write-Host '  手动开启：设置 > 网络和 Internet > 移动热点'
    Write-Host '  Manual:     Settings > Network & Internet > Mobile hotspot'
    Write-Host ''
}

# 列出本机所有可用的 IPv4（去掉回环和 169.254 自动地址）
function Get-AllIPv4 {
    $list = New-Object System.Collections.ArrayList
    try {
        foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            foreach ($ua in $nic.GetIPProperties().UnicastAddresses) {
                if ($ua.Address.AddressFamily -eq 'InterNetwork') {
                    $s = $ua.Address.IPAddressToString
                    if ($s -and $s -notlike '127.*' -and $s -notlike '169.254.*') {
                        [void]$list.Add($s)
                    }
                }
            }
        }
    } catch { }
    return $list
}

# Windows 热点的网段固定是 192.168.137.x，主机一般是 .1
function Get-HotspotIPv4 {
    try {
        $addr = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -like '192.168.137.*' } |
            Select-Object -First 1
        if ($addr) { return $addr.IPAddress }
    } catch { }

    # 兜底：老系统 / 精简系统上 Get-NetIPAddress 可能不可用，直接枚举网卡
    try {
        foreach ($nic in [System.Net.NetworkInformation.NetworkInterface]::GetAllNetworkInterfaces()) {
            foreach ($ua in $nic.GetIPProperties().UnicastAddresses) {
                if ($ua.Address.AddressFamily -eq 'InterNetwork' -and $ua.Address.IPAddressToString -like '192.168.137.*') {
                    return $ua.Address.IPAddressToString
                }
            }
        }
    } catch { }
    return $null
}

# ---------------- 1. 加载 WinRT 组件 ----------------

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null
    $null = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
    $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
    $null = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
} catch {
    Write-Host '[错误] 这台电脑的系统组件里没有 WinRT 网络共享接口，无法自动开热点。'
    Show-ManualHint '系统版本过低或组件被精简'
    exit 1
}

# PowerShell 5.1 里把 WinRT 的 IAsyncOperation 反射成 .NET Task（标准写法）
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

# ---------------- 2. 检查当前网络 ----------------

$profile = $null
try {
    $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
} catch {
    $profile = $null
}

if ($null -eq $profile) {
    Write-Host '[错误] 电脑现在没有联网，Windows 热点需要先有一条能上网的连接。'
    Write-Host '       怎么办：先连上 Wi-Fi，或者插上网线，再运行本脚本。'
    Show-ManualHint '没有可共享的网络连接'
    exit 2
}

$capName = ''
try {
    $capability = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::GetTetheringCapabilityFromConnectionProfile($profile)
    if ($null -ne $capability) { $capName = [string]$capability }
} catch {
    $capName = ''
}

if ($capName -and $capName -ne 'Enabled') {
    $reasons = @{
        'DisabledByGroupPolicy'              = '被组策略禁用（公司/学校统一管理的电脑常见）'
        'DisabledByHardwareLimitation'       = '硬件不支持，多半是这台电脑没有可用的无线网卡'
        'DisabledByOperator'                 = '被网卡驱动或运营商限制'
        'DisabledBySku'                      = '当前 Windows 版本（例如部分精简版）不支持移动热点'
        'DisabledByRequiredAppNotInstalled'  = '系统缺少必需的应用组件'
        'DisabledDueToNonEthernetConnection' = '当前连接类型不允许共享'
        'DisabledBySystemCapability'         = '系统能力被限制'
    }
    $why = $reasons[$capName]
    if (-not $why) { $why = "系统返回 $capName" }
    Write-Host "[错误] 这台电脑开不了热点：$why"
    Write-Host '       如果确实开不了，可以反过来用手机开热点、电脑连手机热点。'
    Show-ManualHint $null
    exit 3
}

# ---------------- 3. 开启热点 ----------------

$manager = $null
try {
    $manager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager]::CreateFromConnectionProfile($profile)
} catch {
    Write-Host '[错误] 创建热点管理器失败，系统热点功能可能不可用。'
    Show-ManualHint $_.Exception.Message
    exit 4
}

if ($null -eq $manager) {
    Write-Host '[错误] 拿不到热点管理器对象，系统热点功能可能被禁用了。'
    Show-ManualHint $null
    exit 4
}

$state = [string]$manager.TetheringOperationalState

if ($state -eq 'On') {
    Write-Host '[提示] 热点本来就是开着的，直接给你访问地址。'
} else {
    Write-Host '正在开启移动热点，请稍等…'
    $result = $null
    try {
        $resultType = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]
        $result = Wait-WinRtOperation ($manager.StartTetheringAsync()) $resultType
    } catch {
        Write-Host '[错误] 调用开热点接口时出错。'
        Show-ManualHint $_.Exception.Message
        exit 5
    }

    if ($null -ne $result -and [string]$result.Status -ne 'Success') {
        $msg = [string]$result.Status
        if ($result.AdditionalErrorMessage) { $msg = $msg + '：' + [string]$result.AdditionalErrorMessage }
        Write-Host "[错误] 热点没有开起来（$msg）。"
        Write-Host '       常见原因：无线网卡被禁用或驱动异常、系统“移动热点”被关掉、安全软件拦截。'
        Show-ManualHint $null
        exit 6
    }

    $state = [string]$manager.TetheringOperationalState
    Write-Host "[完成] 热点状态：$state"
}

# ---------------- 4. 等热点网卡拿到 192.168.137.x ----------------

$ip = $null
for ($i = 0; $i -lt 10; $i++) {
    $ip = Get-HotspotIPv4
    if ($ip) { break }
    Start-Sleep -Milliseconds 500
}

# ---------------- 5. 从 config.json 读端口和 token ----------------

$port = 8000
$token = ''
$cfgPath = Join-Path -Path $PSScriptRoot -ChildPath 'config.json'

if (Test-Path -LiteralPath $cfgPath) {
    try {
        $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($cfg.port) { $port = [int]$cfg.port }
        if ($cfg.token) { $token = [string]$cfg.token }
    } catch {
        Write-Host '[提示] config.json 解析失败，先按默认端口 8000 显示地址。'
    }
}

# ---------------- 6. 打印结果 ----------------

Write-Host ''
Write-Host '=========================================='
Write-Host '  热点已开启'
Write-Host "  热点状态：$state"
Write-Host '=========================================='

if ($ip) {
    Write-Host "  热点 IP：$ip"
    if ($token) {
        Write-Host "  手机浏览器打开：http://${ip}:${port}/?token=$token"
    } else {
        Write-Host "  手机浏览器打开：http://${ip}:${port}/"
        Write-Host '  （还没有 config.json：首次运行 monitor-lan.py 才会生成 token，'
        Write-Host '    之后看同目录的 访问地址.txt 拿带 token 的完整地址）'
    }
} else {
    Write-Host '  暂时没拿到热点 IP（192.168.137.x），热点可能还在启动中。'
    $others = Get-AllIPv4
    if ($others.Count -gt 0) {
        Write-Host ('  本机现有 IPv4：' + ($others -join '、'))
    }
    Write-Host "  等几秒后运行 ipconfig，看“无线局域网适配器”的地址，再用 http://那个IP:${port}/ 访问。"
}

Write-Host ''
Write-Host '  手机要连上这个热点；页面打不开时，先运行 开放防火墙.bat 放行端口。'
Write-Host ''
exit 0
