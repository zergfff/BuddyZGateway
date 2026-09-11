' 静默启动 BuddyZGateway（不弹控制台黑框）
' 用法：把本文件与 BuddyZGateway.py 放同一目录，双击即可；
'      也可用 cscript //nologo launch_silent.vbs 调用。
Option Explicit

Dim fso, sh, here, script

Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
script = fso.BuildPath(here, "BuddyZGateway.py")

' 优先用 pythonw.exe（无窗口）；找不到就退回 python.exe
Dim pythonw, candidates, i, exe
candidates = Array( _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python313\pythonw.exe", _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe", _
    sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python311\pythonw.exe", _
    "C:\Python313\pythonw.exe", _
    "C:\Python312\pythonw.exe", _
    "C:\Python311\pythonw.exe" _
)

pythonw = ""
For i = 0 To UBound(candidates)
    If fso.FileExists(candidates(i)) Then
        pythonw = candidates(i)
        Exit For
    End If
Next

If pythonw = "" Then
    ' 最后尝试 PATH 里的 pythonw
    pythonw = "pythonw.exe"
End If

sh.CurrentDirectory = here
sh.Run """" & pythonw & """ """ & script & """", 0, False
