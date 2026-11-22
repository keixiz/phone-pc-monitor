' 后台启动：用 pythonw 无窗口运行 monitor-lan.py（找不到 pythonw 就退回 python）
' 双击本文件即可，不会弹黑色命令行窗口；要停止请到任务管理器结束 pythonw.exe
Option Explicit

Dim fso, sh, baseDir, q, exePath, pyConsole, cmdLine, rc

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = baseDir
q = Chr(34)

' 1. 文件是否齐全
If Not fso.FileExists(fso.BuildPath(baseDir, "monitor-lan.py")) Then
    MsgBox "同目录下找不到 monitor-lan.py，无法启动。" & vbCrLf & vbCrLf & _
           "请把本文件和 monitor-lan.py 放在同一个文件夹里。" & vbCrLf & vbCrLf & _
           "monitor-lan.py not found in this folder.", 16, "手机监控电脑"
    WScript.Quit 1
End If

' 2. 找 pythonw（无窗口解释器），依次退到 python / pyw / py
exePath = FindExe("pythonw.exe")
If exePath = "" Then exePath = FindExe("python.exe")
If exePath = "" Then exePath = FindExe("pyw.exe")
If exePath = "" Then exePath = FindExe("py.exe")

If exePath = "" Then
    MsgBox "没有找到 Python，无法后台启动。" & vbCrLf & vbCrLf & _
           "怎么办：" & vbCrLf & _
           "1. 打开 https://www.python.org/downloads/ 安装 Python 3.8 或更高版本；" & vbCrLf & _
           "2. 安装第一步务必勾选 Add python.exe to PATH；" & vbCrLf & _
           "3. 装好后重新双击本文件。" & vbCrLf & vbCrLf & _
           "Python not found. Install it from https://www.python.org/downloads/ and check ""Add python.exe to PATH""." & vbCrLf & _
           "Or just run 启动.bat to see the detailed error.", 16, "手机监控电脑"
    WScript.Quit 1
End If

' 3. 先用带控制台的 python 查一遍依赖，避免 pythonw 静默失败（没窗口看不到报错）
pyConsole = FindExe("python.exe")
If pyConsole = "" Then pyConsole = FindExe("py.exe")
If pyConsole = "" Then pyConsole = exePath   ' 至少用要启动的那个解释器检查
rc = sh.Run(q & pyConsole & q & " -c " & q & "import aiohttp, mss, PIL, psutil, pynput" & q, 0, True)
If rc <> 0 Then
    MsgBox "依赖还没装好，后台启动会失败。" & vbCrLf & vbCrLf & _
           "怎么办：先双击同目录的 启动.bat ，首次运行会自动安装依赖；" & vbCrLf & _
           "装好后再双击本文件后台启动。" & vbCrLf & vbCrLf & _
           "Dependencies missing. Run 启动.bat first, then try again.", 48, "手机监控电脑"
    WScript.Quit 1
End If

' 4. 无窗口启动
cmdLine = q & exePath & q & " " & q & fso.BuildPath(baseDir, "monitor-lan.py") & q

On Error Resume Next
sh.Run cmdLine, 0, False
If Err.Number <> 0 Then
    MsgBox "后台启动失败：" & Err.Description & vbCrLf & vbCrLf & _
           "请改用同目录的 启动.bat 启动，窗口里能看到具体报错。" & vbCrLf & vbCrLf & _
           "Failed to start. Please run 启动.bat instead.", 16, "手机监控电脑"
    Err.Clear
    WScript.Quit 1
End If
On Error GoTo 0

WScript.Quit 0

' ---------------------------------------------------------------
' 在 PATH 和常见安装目录里找可执行文件，找不到返回空字符串
' ---------------------------------------------------------------
Function FindExe(exeName)
    Dim paths, p, cand, pyRoot, folder

    FindExe = ""

    ' 先查 PATH 里的每个目录（去掉可能存在的引号）
    paths = sh.ExpandEnvironmentStrings("%PATH%")
    If Len(paths) > 0 Then
        For Each p In Split(paths, ";")
            cand = Trim(Replace(p, q, ""))
            If Len(cand) > 0 Then
                If fso.FileExists(cand & "\" & exeName) Then
                    FindExe = cand & "\" & exeName
                    Exit Function
                End If
            End If
        Next
    End If

    ' PATH 没配好时，扫一下 python.org 安装器的默认目录
    For Each pyRoot In Array( _
            sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python"), _
            sh.ExpandEnvironmentStrings("%ProgramFiles%\Python"), _
            sh.ExpandEnvironmentStrings("%ProgramFiles%\Python311"), _
            sh.ExpandEnvironmentStrings("%ProgramFiles%\Python312"))
        If fso.FolderExists(pyRoot) Then
            If fso.FileExists(pyRoot & "\" & exeName) Then
                FindExe = pyRoot & "\" & exeName
                Exit Function
            End If
            On Error Resume Next
            For Each folder In fso.GetFolder(pyRoot).SubFolders
                If fso.FileExists(folder.Path & "\" & exeName) Then
                    FindExe = folder.Path & "\" & exeName
                    Exit Function
                End If
            Next
            On Error GoTo 0
        End If
    Next
End Function
