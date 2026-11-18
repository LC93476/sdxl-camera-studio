' SDXL Camera Studio - silent launcher (no console window)
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
bat = here & "\\launch.bat"
sh.CurrentDirectory = here
sh.Run """" & bat & """", 0, False