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
;     EVERY .exe in the program folder (subfolders too) is free, so neither the program still saving its state nor a
;     helper it started (Accipio's aria2c takes about four seconds to exit) is killed by CloseApplications, which
;     checks only after PrepareToInstall (Inno Setup's documentation).
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

{ The busy .exe files under Folder (subfolders too), as a list for the log; '' when none is running. }
function UpdateContractBusyIn(const Folder: String): String;
var
  Found: TFindRec;
  Path: String;
begin
  Result := '';
  if not FindFirst(Folder + '\*', Found) then
    exit;
  try
    repeat
      if (Found.Name = '.') or (Found.Name = '..') then
        continue;
      Path := Folder + '\' + Found.Name;
      if (Found.Attributes and FILE_ATTRIBUTE_DIRECTORY) <> 0 then
        Path := UpdateContractBusyIn(Path)
      else if (Lowercase(ExtractFileExt(Found.Name)) <> '.exe') or not UpdateContractExeBusy(Path) then
        Path := '';
      if Path <> '' then
      begin
        if Result <> '' then
          Result := Result + ', ';
        Result := Result + Path;
      end;
    until not FindNext(Found);
  finally
    FindClose(Found);
  end;
end;

<event('PrepareToInstall')>
function UpdateContractWaitForExit(var NeedsRestart: Boolean): String;
var
  Exe, Busy: String;
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
  Busy := UpdateContractBusyIn(ExpandConstant('{app}'));
  while (Busy <> '') and (Waited < 30000) do
  begin
    Sleep(250);
    Waited := Waited + 250;
    Busy := UpdateContractBusyIn(ExpandConstant('{app}'));
  end;
  if Busy <> '' then
    Log(Format('Update contract: still running after %d ms (left to CloseApplications): %s', [Waited, Busy]))
  else if Waited > 0 then
    Log(Format('Update contract: waited %d ms for the programs in %s to stop', [Waited, ExpandConstant('{app}')]))
  else
    Log('Update contract: no program in ' + ExpandConstant('{app}') + ' is running');
end;

<event('CurStepChanged')>
procedure UpdateContractWriteChannel(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    if not SaveStringToFile(ExpandConstant('{app}\update-channel'), 'windows-installer' + #13#10, False) then
      Log('Update contract: could not write ' + ExpandConstant('{app}\update-channel'));
end;
