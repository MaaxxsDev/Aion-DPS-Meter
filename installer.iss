[Setup]
AppId={{9F3E2C7A-5B4D-4A1E-9C8F-1D2E3F4A5B6C}
AppName=Aion DPS Meter
AppVersion=1.0.0
AppPublisher=OriginAion Community
DefaultDirName={autopf}\AionDPSMeter
DefaultGroupName=Aion DPS Meter
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=AionDPSMeter_Setup
Compression=lzma
SolidCompression=yes
SetupIconFile=app_icon.ico
UninstallDisplayIcon={app}\AionDPSMeter.exe
WizardStyle=modern

[Languages]
Name: "german"; MessagesFile: "compiler:Languages\German.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop-Verknüpfung erstellen"; GroupDescription: "Zusätzliche Symbole:"

[Files]
Source: "dist\AionDPSMeter.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Aion DPS Meter"; Filename: "{app}\AionDPSMeter.exe"
Name: "{group}\Deinstallieren"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Aion DPS Meter"; Filename: "{app}\AionDPSMeter.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\AionDPSMeter.exe"; Description: "Aion DPS Meter jetzt starten"; Flags: nowait postinstall skipifsilent
