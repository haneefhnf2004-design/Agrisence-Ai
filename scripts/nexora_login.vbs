' Starts Nexora hidden at Windows login (voice listening) plus OmniRoute.
' Say "Hi Nexora" and the window opens. To turn autostart off, delete
' "Nexora.vbs" from shell:startup (Win+R, type shell:startup).
Set sh = CreateObject("WScript.Shell")
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName))
sh.Environment("Process")("NEXORA_HIDDEN") = "1"
sh.Run "cmd /c omniroute", 0, False
sh.CurrentDirectory = root
sh.Run "cmd /c """ & root & "\START_APA.bat""", 0, False
