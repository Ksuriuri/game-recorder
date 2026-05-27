' Launch game-recorder in the background (hidden window, no console).
Option Explicit

Dim sh, fso, root, exe, cmd, i, arg, args, pathEnv
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
exe = root & "\.venv\Scripts\game-recorder.exe"

If Not fso.FileExists(exe) Then
    WScript.Echo "[错误] 未找到 " & exe & "，请先运行 install.bat"
    WScript.Quit 1
End If

args = ""
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    If InStr(arg, " ") > 0 Or InStr(arg, Chr(9)) > 0 Then
        args = args & " " & Chr(34) & arg & Chr(34)
    Else
        args = args & " " & arg
    End If
Next

pathEnv = root & "\ffmpeg\bin;" & root & "\.venv\Scripts;" & sh.Environment("Process").Item("PATH")
sh.Environment("Process").Item("PATH") = pathEnv
sh.Environment("Process").Item("PYTHONPATH") = root & "\src"
sh.CurrentDirectory = root
cmd = Chr(34) & exe & Chr(34) & args
sh.Run cmd, 0, False
