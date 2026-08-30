!ifndef SourceDir
  !error "SourceDir must point to the built runtime directory"
!endif
!ifndef OutputDir
  !error "OutputDir must point to the release artifact directory"
!endif
!ifndef PackageVersion
  !error "PackageVersion is required"
!endif
!ifndef ReleaseLabel
  !error "ReleaseLabel is required"
!endif
!ifndef QuietUninstallCommand
  !error "QuietUninstallCommand is required"
!endif

Unicode true
Name "Context Atomizer Local"
OutFile "${OutputDir}\ContextAtomizer-Setup-${ReleaseLabel}.exe"
InstallDir "$LOCALAPPDATA\Programs\ContextAtomizer"
InstallDirRegKey HKCU "Software\ContextAtomizer\Installer" "InstallDir"
RequestExecutionLevel user
SetCompressor zlib
ShowInstDetails show
ShowUninstDetails show

!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "Sections.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

!define MUI_ABORTWARNING
!define MUI_UNABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function RequireX64CompatibleWindows
  ${If} ${IsNativeAMD64}
    Return
  ${ElseIf} ${IsNativeARM64}
    ${If} ${AtLeastWin11}
      Return
    ${EndIf}
  ${EndIf}
  SetErrorLevel 2
  IfSilent architecture_abort
  MessageBox MB_ICONSTOP|MB_OK "Context Atomizer requires Windows capable of running x64 applications." /SD IDOK
architecture_abort:
  Abort
FunctionEnd

Function StopExistingRuntime
  IfFileExists "$INSTDIR\atomizer-local-manager.exe" 0 stop_done
  ClearErrors
  ExecWait '"$INSTDIR\atomizer-local-manager.exe" stop' $0
  IfErrors stop_launch_failed
  StrCmp $0 "0" stop_done
  SetErrorLevel 2
  IfSilent stop_abort
  MessageBox MB_ICONSTOP|MB_OK "The existing Context Atomizer runtime refused a clean stop. Manager exit code: $0." /SD IDOK
stop_abort:
  Abort
stop_launch_failed:
  SetErrorLevel 2
  IfSilent stop_launch_abort
  MessageBox MB_ICONSTOP|MB_OK "The existing Context Atomizer runtime could not be stopped." /SD IDOK
stop_launch_abort:
  Abort
stop_done:
FunctionEnd

Function RunManagerStep
  Exch $1
  Exch
  Exch $0
  ClearErrors
  DetailPrint "$1"
  ExecWait '"$INSTDIR\atomizer-local-manager.exe" $0' $2
  IfErrors manager_launch_failed
  StrCmp $2 "0" manager_done
  SetErrorLevel 2
  IfSilent manager_abort
  MessageBox MB_ICONSTOP|MB_OK "$1 Manager exit code: $2." /SD IDOK
manager_abort:
  Abort
manager_launch_failed:
  SetErrorLevel 2
  IfSilent manager_launch_abort
  MessageBox MB_ICONSTOP|MB_OK "$1 The manager could not be launched." /SD IDOK
manager_launch_abort:
  Abort
manager_done:
  Pop $0
  Pop $1
FunctionEnd

Section "Core application" SEC_CORE
  SectionIn RO
  Call StopExistingRuntime
  SetOutPath "$INSTDIR"
  SetOverwrite on
  File "${SourceDir}\atomizer-local-runtime.exe"
  File "${SourceDir}\atomizer-local-manager.exe"
  File "${SourceDir}\atomizer-local-open-library.exe"
  File "${SourceDir}\atomizer-codex-hook.exe"

  CreateDirectory "$SMPROGRAMS\Context Atomizer Local"
  CreateShortCut "$SMPROGRAMS\Context Atomizer Local\Context Atomizer Library.lnk" "$INSTDIR\atomizer-local-open-library.exe" "" "$INSTDIR\atomizer-local-open-library.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "Software\ContextAtomizer\Installer" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "DisplayName" "Context Atomizer Local ${ReleaseLabel}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "DisplayVersion" "${PackageVersion}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "Publisher" "Context Atomizer"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "DisplayIcon" "$INSTDIR\atomizer-local-open-library.exe"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "QuietUninstallString" "$\"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe$\" -NoProfile -NonInteractive -EncodedCommand ${QuietUninstallCommand}"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal" "NoRepair" 1

  Push "install"
  Push "The local runtime could not be initialized."
  Call RunManagerStep
SectionEnd

Section /o "Enable ChatGPT Web capture" SEC_CHATGPT
  Push "install --enable-chatgpt"
  Push "ChatGPT Web capture could not be enabled."
  Call RunManagerStep
  WriteRegDWORD HKCU "Software\ContextAtomizer\Installer" "ChatGPT" 1
SectionEnd

Section /o "Enable Codex capture" SEC_CODEX
  Push "install --enable-codex --codex-hooks $\"$PROFILE\.codex\hooks.json$\" --codex-config $\"$PROFILE\.codex\config.toml$\""
  Push "Codex capture could not be enabled."
  Call RunManagerStep
  WriteRegDWORD HKCU "Software\ContextAtomizer\Installer" "Codex" 1
SectionEnd

Function .onInit
  Call RequireX64CompatibleWindows
  SetShellVarContext current
  ReadRegDWORD $0 HKCU "Software\ContextAtomizer\Installer" "ChatGPT"
  StrCmp $0 "1" 0 +2
    !insertmacro SelectSection ${SEC_CHATGPT}
  ReadRegDWORD $0 HKCU "Software\ContextAtomizer\Installer" "Codex"
  StrCmp $0 "1" 0 +2
    !insertmacro SelectSection ${SEC_CODEX}

  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/CHATGPT=" $R1
  StrCmp $R1 "1" 0 +2
    !insertmacro SelectSection ${SEC_CHATGPT}
  ClearErrors
  ${GetOptions} $R0 "/CODEX=" $R1
  StrCmp $R1 "1" 0 +2
    !insertmacro SelectSection ${SEC_CODEX}
FunctionEnd

Section "Uninstall"
  SetShellVarContext current
  IfFileExists "$INSTDIR\atomizer-local-manager.exe" 0 cleanup_launch_failed
  ClearErrors
  ExecWait '"$INSTDIR\atomizer-local-manager.exe" uninstall --codex-hooks "$PROFILE\.codex\hooks.json" --codex-config "$PROFILE\.codex\config.toml"' $0
  IfErrors cleanup_launch_failed
  StrCmp $0 "0" cleanup_files
  StrCmp $0 "2" cleanup_ambiguous cleanup_error
cleanup_error:
  SetErrorLevel 3
  IfSilent cleanup_files
  MessageBox MB_ICONSTOP|MB_OK "Context Atomizer core cleanup reported an error. The Library database was not deleted." /SD IDOK
  Goto cleanup_files
cleanup_launch_failed:
  SetErrorLevel 3
  IfSilent cleanup_files
  MessageBox MB_ICONSTOP|MB_OK "Context Atomizer core cleanup could not be launched. The Library database was not deleted." /SD IDOK
  Goto cleanup_files
cleanup_ambiguous:
  SetErrorLevel 2
  IfSilent cleanup_files
  MessageBox MB_ICONINFORMATION|MB_OK "Context Atomizer core runtime state was removed, but one ambiguous Codex hook was left unchanged." /SD IDOK

cleanup_files:
  Delete "$SMPROGRAMS\Context Atomizer Local\Context Atomizer Library.lnk"
  RMDir "$SMPROGRAMS\Context Atomizer Local"
  Delete "$INSTDIR\atomizer-local-runtime.exe"
  Delete "$INSTDIR\atomizer-local-manager.exe"
  Delete "$INSTDIR\atomizer-local-open-library.exe"
  Delete "$INSTDIR\atomizer-codex-hook.exe"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\ContextAtomizerLocal"
  DeleteRegKey HKCU "Software\ContextAtomizer\Installer"
SectionEnd
