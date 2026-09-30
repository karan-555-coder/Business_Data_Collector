' Starts the Business Data Collector in the BACKGROUND (hidden window),
' so it cannot be stopped by accidentally closing a console.
' Logs: app\output\app.log (web API) and app\output\worker.log (collections)
' Stop it with: stop_app.bat
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = appDir
py = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\python.exe"
If Not fso.FileExists(py) Then py = "python.exe"
' 0 = hidden window; python.exe (not pythonw) so stdout/stderr stay valid.
sh.Run """" & py & """ -m backend.serve --host 127.0.0.1 --port 8100", 0, False
