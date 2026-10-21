; XynClaim installer. Per-user install, no admin prompt.
; Built by packaging\build.ps1, which passes /DVERSION=x.y.z and /DSRC=<dist\XynClaim>.

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"

!ifndef VERSION
  !error "Pass /DVERSION=x.y.z"
!endif
!ifndef SRC
  !error "Pass /DSRC=path\to\dist\XynClaim"
!endif

!define APP "XynClaim"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}"
!define TASK "XynClaim Daily"

Name "${APP}"
!ifndef OUTFILE
  !define OUTFILE "..\dist\${APP}_${VERSION}_x64-setup.exe"
!endif
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "Software\${APP}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "made by Xyn"

!define MUI_ICON "xynclaim.ico"
!define MUI_UNICON "xynclaim.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start XynClaim"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP}"
VIAddVersionKey "FileDescription" "${APP} installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "Xyn"

Var NoDesktop
Var NoStartMenu
Var ProgressFile

Function .onInit
  StrCpy $NoDesktop 0
  StrCpy $NoStartMenu 0
  StrCpy $ProgressFile ""
  ${GetParameters} $0
  ClearErrors
  ${GetOptions} $0 "/NoDesktop" $1
  IfErrors +2 0
    StrCpy $NoDesktop 1
  ClearErrors
  ${GetOptions} $0 "/NoStartMenu" $1
  IfErrors +2 0
    StrCpy $NoStartMenu 1
  ${GetOptions} $0 "/ProgressFile=" $ProgressFile
  ; An upgrade can't replace files the running app holds open.
  FindWindow $1 "" "${APP}"
  StrCmp $1 0 +4
    MessageBox MB_OK|MB_ICONEXCLAMATION "Close XynClaim before installing." /SD IDOK
    SetErrorLevel 10
    Abort
FunctionEnd

Section "Install"
  ClearErrors
  SetOutPath "$INSTDIR"
  IfErrors install_failed
  ; Clear the old build first so files removed in this version don't linger.
  RMDir /r "$INSTDIR\_internal"
  ; A private status file lets the custom host start measuring after old files are cleared.
  ; No file is written by the normal installer, which doesn't supply this option.
  StrCmp $ProgressFile "" progress_ready
    FileOpen $0 "$ProgressFile" w
    FileWrite $0 "copying"
    FileClose $0
progress_ready:
  ClearErrors
  File /r "${SRC}\*.*"
!ifdef CUSTOM_NOTICES
  File /oname=CUSTOM-INSTALLER-NOTICES.txt "${CUSTOM_NOTICES}"
!endif
  IfErrors install_failed

  ClearErrors
  WriteUninstaller "$INSTDIR\Uninstall.exe"
  StrCmp $NoStartMenu 1 +2
  CreateShortcut "$SMPROGRAMS\${APP}.lnk" "$INSTDIR\${APP}.exe"
  StrCmp $NoDesktop 1 +2
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\${APP}.exe"
  IfErrors install_failed

  ClearErrors
  WriteRegStr HKCU "Software\${APP}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APP}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Xyn"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${APP}.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  IfErrors install_failed
  Goto install_done
install_failed:
  SetErrorLevel 2
  Abort "Setup couldn't write all installation files or registration."
install_done:
SectionEnd

Section "Uninstall"
  ; The schedule points at this exe, so it goes with the app. Hidden, no console flash.
  nsExec::Exec 'schtasks /Delete /TN "${TASK}" /F'
  Pop $0

  Delete "$SMPROGRAMS\${APP}.lnk"
  Delete "$DESKTOP\${APP}.lnk"
  ; Only what the installer put there: the folder may be one the user picked, like Documents.
  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APP}.exe"
  Delete "$INSTDIR\THIRD-PARTY-NOTICES.txt"
  Delete "$INSTDIR\CUSTOM-INSTALLER-NOTICES.txt"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APP}"
  ; Settings, history and receipts in %LOCALAPPDATA%\XynClaim are kept on purpose,
  ; so a reinstall keeps streak history. README says how to remove them.
SectionEnd
