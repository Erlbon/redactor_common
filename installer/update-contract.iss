#if 0
; (These notes sit in "#if 0": included after the app's [Code], a ";" line would be read as Pascal.)
;
; redactor_common/installer/update-contract.iss: the family's installer contract (docs/update-framework.md, section 3).
;
; Every app's Inno Setup script includes this file at its END, so what an update needs from an installer cannot drift
; per app:
;
;   #define UpdateExe "Accipio.exe"            ; the program's exe in {app}
;   #define UpdateQuitArgs "--quit"            ; optional: how the running program is asked to stop and save its state
;   #define UpdateQuitCheck "InstalledKnowsQuit"   ; optional: a Boolean function in the app's [Code] that says whether
;                                              ; the installed copy understands UpdateQuitArgs
;   #include UpdateContract                    ; the path, passed by the build: /DUpdateContract=<this file>
;                                              ; (python -m redactor_common.release contract-path prints it)
;
; What it does:
;   * /relaunch=yes (selfupdate.installer_command) starts the program again when a silent update is done. Dossier's
;     old switch /RESTARTAPP=1 is honoured too, for its copies from before this contract (one transition).
;   * It writes {app}\update-channel = windows-installer, so the program knows it was installed and updates by
;     running the next installer (the folder build itself carries "windows-zip").
;   * Before files are replaced it asks a running program to stop (UpdateQuitArgs), then waits up to 30 seconds until
;     the program's exe is free, so a program still saving its state is not killed by CloseApplications.
;
; It uses Inno Setup 6's event attributes, so the app keeps its own event functions. Those attribute implementations
; run BEFORE the app's own (tests\test_update_contract.py): asking the program to stop belongs here, not in the app's
; PrepareToInstall.
#endif

#ifndef UpdateExe
  #error Define UpdateExe (the program's exe name) before including the update contract
#endif

[Run]
Filename: "{app}\{#UpdateExe}"; Flags: nowait; Check: UpdateContractRelaunch

[UninstallDelete]
Type: files; Name: "{app}\update-channel"

[Code]
function UpdateContractRelaunch: Boolean;
begin
  Result := WizardSilent and ((Lowercase(ExpandConstant('{param:relaunch|no}')) = 'yes')
    or (ExpandConstant('{param:RESTARTAPP|0}') = '1'));
end;

{ True while a running program holds its exe (an exclusive open fails). }
function UpdateContractExeBusy(const FileName: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := False;
  if not FileExists(FileName) then
    exit;
  try
    Stream := TFileStream.Create(FileName, fmOpenReadWrite or fmShareExclusive);
    Stream.Free;
  except
    Result := True;
  end;
end;

<event('PrepareToInstall')>
function UpdateContractWaitForExit(var NeedsRestart: Boolean): String;
var
  Exe: String;
  Code, Waited: Integer;
begin
  Result := '';
  Exe := ExpandConstant('{app}\{#UpdateExe}');
  if not FileExists(Exe) then
    exit;
#ifdef UpdateQuitArgs
  #ifdef UpdateQuitCheck
  if {#UpdateQuitCheck} then
  #endif
  begin
    Log('Update contract: asking the program to stop: ' + Exe + ' {#UpdateQuitArgs}');
    Exec(Exe, '{#UpdateQuitArgs}', '', SW_HIDE, ewWaitUntilTerminated, Code);
  end;
#endif
  Waited := 0;
  while UpdateContractExeBusy(Exe) and (Waited < 30000) do
  begin
    Sleep(250);
    Waited := Waited + 250;
  end;
  if Waited > 0 then
    Log(Format('Update contract: waited %d ms for %s to stop', [Waited, Exe]))
  else
    Log('Update contract: ' + Exe + ' is not running');
end;

<event('CurStepChanged')>
procedure UpdateContractWriteChannel(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    if not SaveStringToFile(ExpandConstant('{app}\update-channel'), 'windows-installer' + #13#10, False) then
      Log('Update contract: could not write ' + ExpandConstant('{app}\update-channel'));
end;
