' tray.vbs - start the Aether Remote PC app (tray icon) with no console.
'
' Used when running from source. The packaged build has no use for this:
' the installer's shortcut points straight at the exe.
'
' Nothing here is allowed to name a particular machine - it finds both the
' interpreter and the script relative to itself or from the usual places.

Option Explicit
Dim sh, fso, py, app, here

Set sh   = CreateObject("WScript.Shell")
Set fso  = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)

app = here & "\tray.py"
If Not fso.FileExists(app) Then WScript.Quit 3

py = FindPython(fso, sh)
If py = "" Then WScript.Quit 2

sh.Run """" & py & """ """ & app & """", 0, False

Function FindPython(fso, sh)
  FindPython = PickPython(fso, sh, here)
End Function

' Shared by tray.vbs and watchdog.vbs - keep the two copies identical.
'
' Newest version first, and BOTH install roots checked per version before
' dropping to an older one. The tray and the server must land on the same
' interpreter; picking by location instead of by version is how they end up
' split across two Pythons.
Function PickPython(fso, sh, folder)
  Dim vers, roots, v, r, c, lad
  lad = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python"

  ' A python sitting beside us wins outright - that is a bundled copy.
  c = folder & "\python\pythonw.exe"
  If fso.FileExists(c) Then
    PickPython = c
    Exit Function
  End If

  vers = Array("314", "313", "312", "311")
  For Each v In vers
    roots = Array("C:\Python" & v & "\pythonw.exe", lad & v & "\pythonw.exe")
    For Each r In roots
      If fso.FileExists(r) Then
        PickPython = r
        Exit Function
      End If
    Next
  Next

  ' Last resort: whatever is on PATH. Run will resolve it.
  PickPython = "pythonw.exe"
End Function
