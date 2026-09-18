' watchdog.vbs - make sure the Aether Remote supervisor is running.
'
' Run from the "Aether PC Remote" scheduled task (at logon + every 5 minutes).
'
' It launches the supervisor unconditionally. That is safe because the
' supervisor holds a named mutex and a second copy exits immediately - which
' is a far better test of "already running" than the one this script used to
' do. It read netstat and looked for :8787 anywhere in the output, so a
' socket left in TIME_WAIT by a server that had just died still counted as
' healthy, and recovery was skipped for minutes at exactly the wrong moment.
'
' Three layers of recovery:
'   supervise.py  respawns the SERVER within ~7s if it dies, and the TRAY
'   this task     respawns the SUPERVISOR within 5 min if that dies
'
' The task runs in the INTERACTIVE session on purpose - a session-0 service
' cannot capture the screen or inject input, which would break the desktop
' streaming entirely.
'
' The packaged build does not use this file: the task runs
' AetherRemote.exe --supervise directly.

Option Explicit
Dim sh, fso, py, app, here

Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' Both found relative to this script, so the folder can move or be installed
' anywhere without editing anything.
here = fso.GetParentFolderName(WScript.ScriptFullName)
app  = here & "\supervise.py"
If Not fso.FileExists(app) Then WScript.Quit 3

py = FindPython(fso, sh, here)
If py = "" Then WScript.Quit 2

' 0 = hidden window, False = don't wait
sh.Run """" & py & """ """ & app & """", 0, False
WScript.Quit 0

' Shared by tray.vbs and watchdog.vbs - keep the two copies identical.
'
' Newest version first, and BOTH install roots checked per version before
' dropping to an older one. The tray and the server must land on the same
' interpreter; picking by location instead of by version is how they end up
' split across two Pythons.
Function FindPython(fso, sh, folder)
  Dim vers, roots, v, r, c, lad
  lad = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python"

  c = folder & "\python\pythonw.exe"
  If fso.FileExists(c) Then
    FindPython = c
    Exit Function
  End If

  vers = Array("314", "313", "312", "311")
  For Each v In vers
    roots = Array("C:\Python" & v & "\pythonw.exe", lad & v & "\pythonw.exe")
    For Each r In roots
      If fso.FileExists(r) Then
        FindPython = r
        Exit Function
      End If
    Next
  Next

  FindPython = "pythonw.exe"
End Function
