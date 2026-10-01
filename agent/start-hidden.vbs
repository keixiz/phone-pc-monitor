' monitor-agent 无窗口启动脚本
' 双击这个文件就能后台运行，不弹黑色命令行窗口
Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
' 优先用 pythonw（无控制台窗口）
ws.Run "pythonw monitor-agent.py", 0, False
