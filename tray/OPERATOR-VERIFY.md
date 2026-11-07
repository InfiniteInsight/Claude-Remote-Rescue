# Operator verification (Windows)

The tray app's Electron-runtime behaviour needs a real Windows desktop with a
GUI and a WSL distro that has crr installed: the tray icon, the window, the
embedded dashboard view, `wsl.exe` calls, login items, sleep and resume. CI
runs on ubuntu and can't drive any of that. The pure logic (poller, hold
parity, contracts, view-models, navigation decisions, the Electron wiring
against fakes) is unit-tested; everything below is checked by hand. The format
follows the Qwen launcher's `OPERATOR-VERIFY.md`: numbered checks, each with
steps and an expected result.

## Before you start

**Steps marked ⚠ end every WSL process, including tmux and Claude sessions.**
That is `wsl --shutdown`, and anything that runs it for you (`crr holder ...
--shutdown`). Save your work first, and run those steps only when the
sessions are disposable or you trust crr's revival (the watchdog normally
revives them; it is not guaranteed). Checks 5, 6, 7 and 21 are the ⚠ ones. Check
3 kills a session's `claude`, so it uses a throwaway session.

A check that fails becomes an issue or a fix PR. The two known gaps are marked
"Known gap" and are not failures.

## Setup

**The build is a folder, not an installer.** From WSL (wine and xvfb needed, as
for the Qwen launcher), in `tray/`:

```
npm install
WINEPREFIX=/path/to/fresh/dir WINEDEBUG=-all xvfb-run -a npm run package:win
```

It uses a fresh wine prefix because a broken `~/.wine` can fail the build
(`could not load kernel32.dll`); `~/.wine` is left alone. Pick any empty
directory for `WINEPREFIX`; wine creates the prefix there on first use.

This writes `tray/release/win-unpacked/CRR.exe` plus its support files. The
folder is gitignored.

**Install.** Copy the whole `tray/release/win-unpacked` folder to a Windows
folder, for example `C:\Tools\CRR\`, and run `CRR.exe` from the copy. Don't run
it from the `\\wsl$` path: login items and single-instance behave differently
there. Nothing else is installed or registered; deleting the folder removes the
app, except its data folder (next paragraph).

**Full reset.** Quit the app, then delete its userData folder,
`%APPDATA%\crr-tray` (the folder that holds `crr-tray.json`). That clears the
window bounds, the chosen distro and the dashboard login, which lives in the
`persist:crr-dashboard` partition inside that folder. Do this before a check
that needs a first-run or logged-out state.

**Start it hidden.** `CRR.exe --hidden` starts in the tray with no window. The
Start at login item registers exactly this.

**You need:** crr installed in a WSL distro (`crr doctor` is clean), crr's
Windows pieces if you use the boot holder (`crr reachable-at-boot --install`),
a phone on the same tailnet for the QR check, and optionally Sysinternals
Process Monitor for the `wsl.exe` checks. In Process Monitor filter on
`Process Name is wsl.exe`, `Command Line contains  -d `; a quiet list there is
the pass condition wherever a check says "no `wsl.exe -d` call".

Commands below that start with `crr` run inside the WSL distro unless noted.
Run `wsl -l --running` from a Windows terminal.

## A. Tray icon and states

1. **The icon appears.** Start `CRR.exe` with crr running. A tray icon appears
   in the notification area (check the overflow chevron). Hover it: the tooltip
   is the status line. *Expected:* the "running" icon, and a status line like
   `CRR running · N sessions, M crashed`.

2. **Icon follows `crr service stop` and `start`.** Run `crr service stop`.
   Within one poll (about 5 to 7 s) the icon becomes "stopped" and the status
   line says `CRR stopped`. Run `crr service start`. *Expected:* the icon goes
   back to "running" within a poll. Keep-awake being off on its own never turns
   the icon to "stopped". **tmux sessions survive `crr service stop`:** run
   `tmux ls` before and after, and the list is the same both times.

3. **A crashed session shows "needs attention".** Use a throwaway session
   (start one in a new tmux window), not one you care about. Make it crash by
   killing that session's `claude` and let the watchdog notice, or use a
   session that is already crashed or running elsewhere. *Expected:* the "needs attention"
   icon and `N sessions, M crashed` with M at least 1. After the session is
   revived or dismissed the icon returns to "running".

4. **Menu content and order.** Right-click the icon. *Expected, top to bottom:*
   a greyed status line; Open CRR; Open dashboard in browser (greyed while the
   dashboard is down); Start CRR or Stop CRR; Start at login; Quit tray app.
   Left-clicking the icon opens the window.

## B. Never wake WSL

5. **After `wsl --shutdown`.** ⚠ Ends every WSL process, tmux and Claude
   sessions included; see Before you start. With the app open (window on the Control tab),
   run `wsl --shutdown` in a Windows terminal. *Expected:* the icon becomes
   "WSL down" and the status line says `WSL is stopped` within a poll.
   `wsl -l --running` **stays empty for 2 minutes** with the app open. The app
   never boots the distro back. (If the crr boot holder is installed it may
   re-arm WSL itself within a minute: pause it first with `crr holder pause 10
   --shutdown`, see check 6, which has the same ⚠.)

6. **No `wsl.exe -d` call under `crr holder pause 10`.** ⚠ `--shutdown` ends
   every WSL process, tmux and Claude sessions included; crr's watchdog
   normally revives them when WSL next starts; confirm with `crr status`. Run
   `crr holder pause 10 --shutdown`. With Process Monitor running, watch for 2 minutes. *Expected:* no
   `wsl.exe` process with `-d` in its command line starts, `wsl -l --running`
   stays empty, and the status line says `WSL is stopped`. The only `wsl.exe`
   calls are the plain `-l --running -q` list. After about 10 minutes the
   holder re-arms and the icon recovers by itself. If WSL is still up while a
   hold is in place (pause without `--shutdown`), the status line says `crr
   holder is holding WSL down — not checking crr` and again no `-d` call
   appears.

7. **A hold survives sleep and resume (stop until reboot).** Run `crr holder
   stop --until-reboot`. Adding `--shutdown` also takes WSL down, but ⚠ that
   ends every WSL process, tmux and Claude sessions included; leave it off
   if the sessions matter (the hold is then shown as the hold line, not `WSL is
   stopped`). Confirm the
   status line shows the hold or `WSL is stopped`. Now sleep the machine and
   wake it. *Expected:* after wake the tray still shows the hold (or `WSL is
   stopped`), `wsl -l --running` stays empty if you shut WSL down, and Process
   Monitor shows no `wsl.exe -d` call, not on resume and not on the first poll
   after it. Clean up with `crr holder resume` (an explicit action, it may boot
   WSL). With a plain `crr holder stop` the same holds; `--until-reboot` is the
   case that lifts at the next Windows restart.

## C. Control tab

8. **Start CRR boots WSL and starts the units.** With WSL down (after check 5
   or 6, or after letting WSL idle out), click Start CRR in the tray menu (or on
   the Control tab). *Expected:* WSL boots, the watchdog, dashboard and
   keep-awake units start, and the icon goes "running". Whether the sessions
   come back is crr's revival, not this check; tmux survival is check 9.

9. **Stop CRR stops the units, tmux sessions survive.** With a few live
   sessions, run `tmux ls` in WSL and note the list. Click Stop CRR, then run
   `tmux ls` again. *Expected:* the rows go to stopped, the icon goes to
   "stopped", and `tmux ls` lists exactly the same sessions as before. The
   button becomes Start CRR.

10. **Rows, picker, Repair and Refresh.** On the Control tab:
    - the rows Watchdog (revives sessions), Dashboard and Keep-awake each show
      a dot that matches `crr service status` (stop one unit with `systemctl
      --user stop` and watch its dot follow);
    - Repair appears only when the units are not installed, and then installs them;
    - Refresh forces a poll;
    - the WSL distro picker lists `wsl -l -q`, and choosing another distro
      persists across an app restart and boots that distro (an explicit
      action, so this is allowed to start WSL).
    *Expected:* all of the above.

11. **The QR code scans.** In the Phone section, a QR code and its URL appear
    while WSL is running. Scan it with a phone on the tailnet. *Expected:* the
    phone opens the crr dashboard. The QR refreshes about every 60 s only while
    the window is shown on the Control tab, and **not** while WSL is stopped
    or held (no `-d` call).

## D. Sessions tab (the embedded dashboard)

12. **The tab shows the dashboard.** With CRR running, open the Sessions tab.
    *Expected:* the crr dashboard, the same page as in the browser.

13. **Layout.** The dashboard sits directly under the tab bar, with no gap and
    not covering the tabs' bottom line. Resize, maximize, restore and snap the
    window, and repeat at 125% and 150% display scaling. *Expected:* the view
    follows each time.

14. **Login persists across an app restart.** On the dashboard's passphrase
    page in the tab, log in. Quit with the tray menu's Quit tray app and
    relaunch. *Expected:* the Sessions tab is still logged in.

15. **Controls work.** Session actions (revive, close, kick and so on) work
    exactly as in the browser.

16. **External links open the default browser.** In the Re-authenticate Claude
    Code modal, click the sign-in link (or any other off-site link).
    *Expected:* it opens in the **default browser**; no Electron window opens;
    the tab stays on the dashboard. Alt+Left and the mouse back button don't
    leave the dashboard either.

17. **Stopped state.** Stop CRR from the tray. *Expected:* within one poll
    (about 5 to 7 s) the Sessions tab shows "CRR is stopped" with a Start CRR
    button. Start CRR brings the dashboard back.

18. **Loading, not stuck.** Stop crr-web (`systemctl --user stop crr-web.service`),
    then start it, while on the Sessions tab. *Expected:* while the page can't
    load, the panel says "Loading the dashboard…" rather than a blank white
    area, and the dashboard returns at the next poll after crr-web answers.

19. **Tab switch keeps the page.** Scroll the dashboard, switch to Control (the
    dashboard disappears and Control is fully visible), then switch back.
    *Expected:* the same scroll position, not a reload.

20. **Port change.** Change `dashboard_port` and apply it (reinstall plus
    restart crr-web). *Expected:* within a poll the Sessions tab shows the
    dashboard on the new port, and Alt+Left or the mouse back button don't
    return to the old port.

21. **Never wakes WSL.** ⚠ Ends every WSL process, tmux and Claude sessions
    included; see Before you start. With the Sessions tab open, run
    `wsl --shutdown`. *Expected:* the tab shows "CRR is stopped", and `wsl -l --running` stays
    empty for 2 minutes with the window open on Sessions.

22. **Hold takes the dashboard off screen.** Run `crr holder pause 10` while on
    Sessions. *Expected:* the dashboard goes off screen at the next poll and no
    `wsl.exe -d` call follows.

23. **Sleep and resume on Sessions.** Sleep and resume the machine while on
    Sessions. *Expected:* the dashboard is off screen until a fresh poll
    lands, then returns. No `-d` call between resume and that poll.

24. **Clipboard.** A dashboard Copy button still copies (paste it somewhere).
    The clipboard permission is denied, so this relies on the page's
    `execCommand('copy')` fallback. Ctrl+C and Ctrl+V still work in the
    dashboard's inputs.

## E. Window behaviour and lockdown

25. **Closing the window hides it.** Click the window's X. *Expected:* the
    window disappears, the icon stays, and the process keeps running (Task
    Manager). Reopen from the tray (Open CRR) while on Sessions: the dashboard
    is shown. Minimize and restore: the same.

26. **Quit tray app ends the app.** Choose Quit tray app. *Expected:* the icon
    disappears and no `CRR.exe` process remains in Task Manager. crr and its
    units in WSL keep running.

27. **No DevTools, no app menu.** Click into the dashboard, then into the tab
    bar, and press Ctrl+Shift+I and F12 in each. *Expected:* nothing opens. Alt
    shows no menu bar.

28. **No zoom or reload of the app page.** Click the tab bar, then press
    Ctrl+=, Ctrl+- and Ctrl+mouse-wheel. *Expected:* the tab bar stays the same
    size and the dashboard doesn't overlap it. Ctrl+R does nothing.

29. **No permission prompts** appear at any point (camera, microphone,
    location, notifications, clipboard-read, and so on).

30. **No device is handed over.** The dashboard view has no DevTools, so run
    these from a test page or a temporary build: `navigator.bluetooth.
    requestDevice({acceptAllDevices:true})`, `navigator.hid.requestDevice(
    {filters:[]})`, `navigator.usb.requestDevice({filters:[]})` and
    `navigator.serial.requestPort()`. *Expected:* each rejects, with no picker
    shown and no device chosen, even with a Bluetooth device nearby.

31. **No client certificate.** *Covered by unit tests; no home test.* The
    dashboard view answers `select-client-certificate` by cancelling, so it
    never presents a certificate (`sessionsWiring.test.ts`, the `select-client-certificate` handler). A real check needs a TLS server that requests a client
    certificate, a certificate installed in the Windows store, and a page that
    fetches a subresource from that server. The dashboard is plain http on
    loopback and loads no such resource, so there is nothing to drive without
    building a test page and server just for this.

## F. Background behaviour

32. **The menu stays open across polls.** Right-click the tray icon to open the
    menu and leave it open for 15 seconds, without touching it. *Expected:* it
    stays open through the 5 s polls. It may close **once** when the status
    line itself changes (the session counts change); that is a known limit of
    Windows tray menus, not a failure. Run it again with nothing changing to
    confirm it doesn't close.

33. **No console windows flash.** Watch the screen and the taskbar for a minute
    while the app polls (window shown, then hidden to the tray). *Expected:*
    no black console window ever flashes. All `wsl.exe` calls use
    `windowsHide`.

34. **Start at login launches hidden.** Tick Start at login in the tray menu
    (it exists in the packaged build only). Check Task Manager's Startup apps
    tab for CRR. Sign out and back in, or restart. *Expected:* CRR starts with
    the tray icon and **no window**. Run `CRR.exe --hidden` by hand for the same
    result without signing out. Untick the item: the startup entry goes away.

## G. Known gaps (not failures)

35. **Known gap, #186: "disable login" doesn't work in the tray.** The
    dashboard's "disable login" control uses `window.prompt()`
    (`crr/core/page.html`), which Electron doesn't implement, so it does
    nothing in the Sessions tab. *Expected:* the button does nothing here and
    works from a browser. Use the tray menu's Open dashboard in browser for it.
    Marked as a known gap, tracked in #186.

36. **Known gap: no custom executable icon.** `CRR.exe` in Explorer carries
    Electron's default icon. The tray icons (running, attention, stopped, WSL
    down) are drawn in code and are not affected.
