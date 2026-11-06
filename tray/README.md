# crr-tray

A Windows tray app for [Claude-Remote-Rescue (crr)](../README.md). It shows
crr's state in the system tray, opens the crr dashboard in a window, and starts
and stops crr in WSL. It reaches crr only through the dashboard's HTTP port and
`wsl.exe -d <distro> -e <crr path> ...`. It is built with Electron and
TypeScript, and reuses code from the owner's Qwen Remote Launcher (see
`NOTICE`).

## Develop

```
npm install
npm test            # vitest
npm run typecheck   # tsc --noEmit
npm start           # build and launch; run this from Windows
```

## Package

From WSL with wine installed, the same way the Qwen launcher is built:

```
npm run package:win
```

This writes `release/win-unpacked`. Copy that folder to Windows and run
`CRR.exe` from the copy.
