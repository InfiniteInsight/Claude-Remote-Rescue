# Claude-Remote-Rescue

Keep your Claude Code sessions alive — and rescue them from your phone —
when terminals, shells, or whole machines die.

When a terminal window closes, a laptop reboots, or a VM runs out of
memory mid-conversation, Claude-Remote-Rescue notices, revives each
session (`claude --resume`) into tmux, and gives you a tailnet-only web
dashboard to restore, kick, close, or dismiss any session from any
device. It also tells you *why* things died (journald, macOS logs,
Windows Event Log — translated to plain English).

**Status: design phase.** See [DESIGN.md](DESIGN.md) and
[ROADMAP.md](ROADMAP.md). The design is a ground-up OSS rewrite of a
private Windows/WSL implementation that survived two real outages in
production (a Windows-Update reboot and a WSL OOM crash) with zero lost
conversations — every design decision marked **[lesson]** in DESIGN.md
was paid for the honest way.

Targets: headless Linux, macOS (Terminal.app / iTerm2), Linux desktop
(gnome-terminal / konsole / kitty / wezterm), Windows/WSL. Shells: zsh,
bash, fish.

Not affiliated with Anthropic. MIT licensed.
