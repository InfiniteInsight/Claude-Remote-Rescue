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
WINEPREFIX=/path/to/fresh/dir WINEDEBUG=-all xvfb-run -a npm run package:win
```

It uses a fresh wine prefix because a broken `~/.wine` can fail the build
(`could not load kernel32.dll`); `~/.wine` is left alone. Use any empty
directory for `WINEPREFIX`.

This writes `release/win-unpacked`. Copy that folder to Windows and run
`CRR.exe` from the copy.

## Verify on Windows

CI can't drive a Windows GUI, so the Electron runtime behaviour (tray icon,
window, the embedded dashboard, sleep and resume, login items) is checked by
hand. [OPERATOR-VERIFY.md](OPERATOR-VERIFY.md) is the checklist, including how to
install the build and start it hidden (`CRR.exe --hidden`).
