#!/usr/bin/env bash
#
# bootstrap.sh — one-shot, cross-OS setup for Claude-Remote-Rescue (crr).
#
# Targets: headless Linux, Linux desktop, macOS, and Windows/WSL (run this
# INSIDE the WSL distro). All four share bash. Native Windows PowerShell is
# not a target: the Windows story is WSL, so run this from a WSL shell.
#
# What it does, in order (each step is logged, and nothing risky runs without
# a confirmation unless --yes is given):
#   1. Detect the OS.
#   2. Check prerequisites (Python >= 3.11, tmux, pipx; git when installing
#      from git) and, if one is missing, offer to install it.
#   3. Install crr with pipx (isolated venv, zero runtime deps).
#   4. Install the shell shim into the right rc file (idempotent — re-running
#      replaces the managed block, it never duplicates).
#   5. Install the platform services (watchdog + dashboard + keep-awake):
#      systemd on Linux/WSL-with-systemd, launchd on macOS, or the Windows
#      Scheduled Tasks on WSL without in-distro systemd.
#   6. Offer to expose the dashboard on your tailnet (tailscale) — this is
#      explained in full and requires an explicit yes.
#   7. Run `crr doctor` and print a summary + next steps.
#
# It is deliberately conservative: it never force-installs a system package,
# never touches anything outside the crr state dir + your shell rc + the
# user-level service units, and it tells you exactly what it did or why it
# stopped. Safe to re-run to refresh an existing install.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/InfiniteInsight/Claude-Remote-Rescue/main/bootstrap.sh | bash
#   # or, from a checkout (installs that checkout):
#   ./bootstrap.sh
#
# Flags:
#   -y, --yes          Assume yes to every confirmation (unattended).
#   -n, --dry-run      Print what would run; change nothing.
#   --shell S          Force the shim shell: bash | zsh | fish (default: detect).
#   --tailscale        Offer the tailnet-dashboard step (default: ask if tailscale is present).
#   --no-tailscale     Skip the tailnet step entirely.
#   --from-local       Install from this checkout (default when run from one).
#   --from-git         Install from the upstream git repo (default when not in a checkout).
#   --git-ref REF      Git ref to install when using --from-git (default: default branch).
#   -h, --help         Show this help.

set -euo pipefail

# ---- output helpers -------------------------------------------------------
if [ -t 1 ]; then
  C_INFO=$'\033[0;36m'; C_OK=$'\033[0;32m'; C_WARN=$'\033[0;33m'
  C_ERR=$'\033[0;31m'; C_DIM=$'\033[0;2m'; C_R=$'\033[0m'
else
  C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_R=""
fi
info() { printf '%s==>%s %s\n' "$C_INFO" "$C_R" "$*"; }
ok()   { printf '  %s[ok]%s %s\n'   "$C_OK" "$C_R" "$*"; }
note() { printf '  %s[note]%s %s\n' "$C_DIM" "$C_R" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$C_WARN" "$C_R" "$*" >&2; }
err()  { printf '  %s[err]%s %s\n'  "$C_ERR" "$C_R" "$*" >&2; }
die()  { err "$*"; exit 1; }
step() { printf '\n%s%s%s\n' "$C_INFO" "$*" "$C_R"; }

have() { command -v "$1" >/dev/null 2>&1; }

# ---- flags ----------------------------------------------------------------
ASSUME_YES=0
DRY_RUN=0
SHELL_NAME_OVERRIDE=""
TAILSCALE=""            # "" = auto (ask if present), 1 = offer, 0 = skip
INSTALL_SOURCE="auto"   # auto | local | git
GIT_REF=""

usage() {
  # Print the leading comment block (after the shebang, up to `set -euo`),
  # stripping the leading "# " so it reads as plain text.
  awk 'NR==1{next} /^set -euo/{exit} {sub(/^# ?/, ""); print}' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    -y|--yes) ASSUME_YES=1 ;;
    -n|--dry-run) DRY_RUN=1 ;;
    --shell) [ $# -ge 2 ] || die "--shell needs a value"; SHELL_NAME_OVERRIDE="$2"; shift ;;
    --tailscale) TAILSCALE=1 ;;
    --no-tailscale) TAILSCALE=0 ;;
    --from-local) INSTALL_SOURCE=local ;;
    --from-git) INSTALL_SOURCE=git ;;
    --git-ref) [ $# -ge 2 ] || die "--git-ref needs a value"; GIT_REF="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

# ---- confirm: the single place a risky action is gated --------------------
# Reads from /dev/tty so it still works when this script is piped in
# (curl | bash): stdin is the pipe, but the user's terminal is still a tty.
# With no tty AND no --yes it returns "no" — the safe default, so an
# unattended run never does the thing that needs a human to say so.
confirm() {
  # $1 = the question
  if [ "$ASSUME_YES" = 1 ]; then
    printf '  %s[--yes]%s assuming yes: %s\n' "$C_DIM" "$C_R" "$1"
    return 0
  fi
  if [ ! -r /dev/tty ]; then
    printf '  %s%s [y/N]%s no terminal to confirm — treating as no (pass --yes to allow)\n' "$C_WARN" "$1" "$C_R"
    return 1
  fi
  printf '  %s%s [y/N] ' "$C_WARN" "$1"
  local ans=""
  read -r ans < /dev/tty || ans=""
  case "$ans" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# run CMD...            — execute (or print, under --dry-run) an argv command.
run() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s ' "$C_DIM" "$C_R"; printf '%q ' "$@"; printf '\n'
    return 0
  fi
  printf '  %s$%s %s\n' "$C_DIM" "$C_R" "$*"
  "$@"
}

# run_sh "shell string" — execute a compound command string (for installs).
run_sh() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s %s\n' "$C_DIM" "$C_R" "$1"
    return 0
  fi
  printf '  %s$%s %s\n' "$C_DIM" "$C_R" "$1"
  bash -c "$1"
}

# ---- OS detection ---------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
  Darwin) OS_KIND="macos" ;;
  Linux)  OS_KIND="linux" ;;
  *) die "unsupported OS: $OS (bootstrap.sh targets Linux, macOS, and WSL)" ;;
esac

IS_WSL=0
if [ "$OS_KIND" = "linux" ] && { [ -n "${WSL_DISTRO_NAME:-}" ] \
      || grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; }; then
  IS_WSL=1
fi

case "$OS_KIND" in
  macos) PLATFORM_LABEL="macOS" ;;
  linux) [ "$IS_WSL" = 1 ] && PLATFORM_LABEL="Windows/WSL" || PLATFORM_LABEL="Linux" ;;
esac

info "Claude-Remote-Rescue bootstrap"
note "platform: $PLATFORM_LABEL"
[ "$DRY_RUN" = 1 ] && note "dry-run: nothing will be changed"

# The package manager used only for the *optional* prereq installs below.
# crr itself installs via pipx (user-level, no package manager, no sudo).
PM=""
if have brew; then PM="brew"
elif have apt-get; then PM="apt"
elif have dnf; then PM="dnf"
elif have pacman; then PM="pacman"
elif have apk; then PM="apk"
fi

# Suggest a command for installing $1 on this platform. Prints the command,
# or nothing if there's no sensible one (in which case the caller explains
# manually). This is a best-guess per platform, not a guarantee — the message
# always says so.
suggest_install() {
  # $1 = what to install (python|tmux|pipx|git)
  case "$1" in
    python)
      case "$PM" in
        brew) echo "brew install python@3.12" ;;
        apt) echo "sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv" ;;
        dnf) echo "sudo dnf install -y python3 python3-pip" ;;
        pacman) echo "sudo pacman -S --noconfirm python python-pip" ;;
        apk) echo "sudo apk add python3 py3-pip" ;;
      esac ;;
    tmux)
      case "$PM" in
        brew) echo "brew install tmux" ;;
        apt) echo "sudo apt-get update && sudo apt-get install -y tmux" ;;
        dnf) echo "sudo dnf install -y tmux" ;;
        pacman) echo "sudo pacman -S --noconfirm tmux" ;;
        apk) echo "sudo apk add tmux" ;;
      esac ;;
    pipx)
      # User-level pip install works everywhere and needs no sudo; the
      # package-manager form is only suggested where it's clearly right.
      case "$PM" in
        brew) echo "brew install pipx" ;;
        apt) echo "python3 -m pip install --user pipx" ;;
        *) echo "python3 -m pip install --user pipx" ;;
      esac ;;
    git)
      case "$PM" in
        brew) echo "brew install git" ;;
        apt) echo "sudo apt-get update && sudo apt-get install -y git" ;;
        *) echo "(install git with your package manager)" ;;
      esac ;;
    tailscale)
      case "$PM" in
        brew) echo "brew install tailscale" ;;
        apt) echo "sudo apt-get update && sudo apt-get install -y tailscale" ;;
        dnf) echo "sudo dnf install -y tailscale" ;;
        *) echo "(install tailscale from https://tailscale.com/download)" ;;
      esac ;;
  esac
}

# ensure CMD [label] [install-cmd] [why]
# Confirm-and-install a prerequisite. Aborts if it's still missing after
# (the caller cannot continue without it).
ensure_prereq() {
  # $1=command to test, $2=human label, $3=install cmd, $4=why it's needed
  if have "$1"; then
    ok "$2: $(command -v "$1")"
    return 0
  fi
  warn "missing prerequisite: $2 ($1) — needed for $4"
  local cmd="$3"
  if [ -z "$cmd" ]; then
    cmd="(no automatic install found for $PLATFORM_LABEL — install '$2' yourself)"
  fi
  if [ "$DRY_RUN" = 1 ]; then
    note "would offer to install: $cmd"
    return 0
  fi
  printf '  suggested: %s\n' "$cmd"
  if confirm "Install $2 now?"; then
    run_sh "$cmd"
    if have "$1"; then
      ok "$2 installed: $(command -v "$1")"
    else
      die "$2 is still not available after that command. Install it manually ($cmd), then re-run this script."
    fi
  else
    die "Declined to install $2. Install it first — $cmd — then re-run this script."
  fi
}

# ---- 2. prerequisites -----------------------------------------------------
step "1/6  Prerequisites"

PYTHON_BIN=""
# Prefer an explicit >=3.11 interpreter, then any python3 that happens to be new enough.
for cand in python3.13 python3.12 python3.11 python3; do
  if have "$cand" && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
    PYTHON_BIN="$(command -v "$cand")"; break
  fi
done
if [ -n "$PYTHON_BIN" ]; then
  ok "Python >= 3.11: $PYTHON_BIN ($("$PYTHON_BIN" --version 2>&1))"
else
  warn "missing prerequisite: Python >= 3.11 (crr requires it to run)"
  PY_CMD="$(suggest_install python)"
  [ -z "$PY_CMD" ] && PY_CMD="(install Python 3.11+ yourself)"
  if [ "$DRY_RUN" = 1 ]; then
    note "would offer to install: $PY_CMD"
  else
    printf '  suggested: %s  %s(distro python may be older; pyenv/deadsnakes if so)%s\n' "$PY_CMD" "$C_DIM" "$C_R"
    if confirm "Install Python now?"; then
      run_sh "$PY_CMD"
      for cand in python3.13 python3.12 python3.11 python3; do
        if have "$cand" && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
          PYTHON_BIN="$(command -v "$cand")"; break
        fi
      done
      [ -n "$PYTHON_BIN" ] || die "Python >= 3.11 still not found. Install it manually, then re-run."
    else
      die "Declined to install Python. crr needs Python >= 3.11. Install it, then re-run."
    fi
  fi
fi

ensure_prereq tmux  "tmux"      "$(suggest_install tmux)"  "reviving crashed sessions into tmux"
ensure_prereq pipx  "pipx"      "$(suggest_install pipx)"  "installing crr in an isolated venv"

# (git is checked in the install-source step below, once we know whether we
# are cloning from git or installing from a local checkout.)

# ---- install source resolution -------------------------------------------
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo "")"
HAS_LOCAL=0
if [ -n "$script_dir" ] && [ -f "$script_dir/pyproject.toml" ] && [ -d "$script_dir/crr" ]; then
  HAS_LOCAL=1
fi
case "$INSTALL_SOURCE" in
  local) [ "$HAS_LOCAL" = 1 ] || die "--from-local given, but this isn't a crr checkout (no pyproject.toml + crr/ here)" ;;
  git)   : ;;
  auto)
    if [ "$HAS_LOCAL" = 1 ]; then INSTALL_SOURCE="local"
    else INSTALL_SOURCE="git"; fi ;;
esac
if [ "$INSTALL_SOURCE" = "git" ]; then
  ensure_prereq git "git" "$(suggest_install git)" "cloning the crr repo for pipx"
fi

# ---- 3. install crr -------------------------------------------------------
step "2/6  Installing crr (pipx)"
if [ "$INSTALL_SOURCE" = "local" ]; then
  PKG_SPEC="$script_dir"
  note "installing from this checkout: $script_dir"
else
  GIT_URL="git+https://github.com/InfiniteInsight/Claude-Remote-Rescue"
  [ -n "$GIT_REF" ] && GIT_URL="$GIT_URL@$GIT_REF"
  PKG_SPEC="$GIT_URL"
  note "installing from git: $GIT_URL"
fi
# --force makes a re-run a refresh, not a failure; --python pins the >=3.11
# interpreter we verified above.
run pipx install --force --python "$PYTHON_BIN" "$PKG_SPEC"
# Put pipx's bin dir on PATH for the rest of this run + for new shells.
CRR_BIN_DIR="$HOME/.local/bin"
if have pipx; then
  CRR_BIN_DIR="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || true)"
  [ -n "$CRR_BIN_DIR" ] || CRR_BIN_DIR="$HOME/.local/bin"
  case ":$PATH:" in
    *":$CRR_BIN_DIR:"*) : ;;
    *) export PATH="$CRR_BIN_DIR:$PATH" ;;
  esac
  # Make ~/.local/bin (or pipx's bin dir) persist for new shells. Idempotent,
  # and it manages its own rc lines separately from the crr shim block.
  if [ "$DRY_RUN" = 0 ]; then
    pipx ensurepath >/dev/null 2>&1 || true
  fi
fi
if ! have crr; then
  if [ "$DRY_RUN" = 1 ]; then
    note "crr not on PATH yet (expected in dry-run; present after the real install)"
  else
    die "crr is not on PATH after install (looked in $CRR_BIN_DIR). Open a new shell and re-run."
  fi
fi
if [ "$DRY_RUN" = 0 ]; then
  ok "crr: $(crr --version 2>/dev/null || echo 'installed')"
fi

# ---- 4. shell shim --------------------------------------------------------
step "3/6  Shell shim"
SHELL_NAME="$SHELL_NAME_OVERRIDE"
if [ -z "$SHELL_NAME" ]; then
  case "$(basename "${SHELL:-/bin/bash}")" in
    bash) SHELL_NAME="bash" ;;
    zsh)  SHELL_NAME="zsh" ;;
    fish) SHELL_NAME="fish" ;;
    *)
      # $SHELL empty/unrecognised: trust the running shell, else default to bash.
      if [ -n "${ZSH_VERSION:-}" ]; then SHELL_NAME="zsh"
      elif [ -n "${FISH_VERSION:-}" ]; then SHELL_NAME="fish"
      else SHELL_NAME="bash"; fi ;;
  esac
fi
case "$SHELL_NAME" in
  bash) RC_FILE="$HOME/.bashrc" ;;
  zsh)  RC_FILE="$HOME/.zshrc" ;;
  fish) RC_FILE="$HOME/.config/fish/config.fish" ;;
  *) die "unsupported shim shell: $SHELL_NAME (use bash, zsh, or fish)" ;;
esac
note "shell: $SHELL_NAME -> $RC_FILE"

SHIM_BEGIN="# >>> crr shim (managed by crr bootstrap.sh — do not edit inside) >>>"
SHIM_END="# <<< crr shim <<<"

install_shim() {
  if [ "$DRY_RUN" = 1 ]; then
    printf '  %s[dry-run]%s would manage the crr shim block in %s\n' "$C_DIM" "$C_R" "$RC_FILE"
    return 0
  fi
  local shim tmp
  shim="$(crr shim "$SHELL_NAME")"
  [ -f "$RC_FILE" ] || : > "$RC_FILE"
  if grep -qF "$SHIM_BEGIN" "$RC_FILE" && ! grep -qF "$SHIM_END" "$RC_FILE"; then
    warn "found a crr shim start marker without its end marker in $RC_FILE; rewriting the dangling block"
  fi
  tmp="$(mktemp)"
  # Drop any existing managed block (start marker .. end marker, or EOF if
  # the end is missing), keeping everything else verbatim.
  awk -v b="$SHIM_BEGIN" -v e="$SHIM_END" '
    $0==b {skip=1; next}
    $0==e {skip=0; next}
    !skip {print}
  ' "$RC_FILE" > "$tmp"
  # Append the fresh block (position at EOF is fine: it only defines
  # functions and registers this shell when the rc is sourced).
  {
    printf '%s\n' "$SHIM_BEGIN"
    printf '%s\n' "$shim"
    printf '%s\n' "$SHIM_END"
  } >> "$tmp"
  mv "$tmp" "$RC_FILE"
  ok "shim installed into $RC_FILE"
}
install_shim

# ---- 5. platform services -------------------------------------------------
step "4/6  Services (watchdog + dashboard + keep-awake)"
SERVICES_LABEL=""
wsl_has_systemd() { have systemctl && [ -d /run/systemd/system ]; }
case "$OS_KIND" in
  macos)
    info "macOS launchd user agents"
    run crr launchd --install
    SERVICES_LABEL="launchd (watchdog + dashboard + keep-awake)"
    ;;
  linux)
    if [ "$IS_WSL" = 1 ] && ! wsl_has_systemd; then
      info "Windows/WSL Scheduled Tasks (this WSL distro has no in-distro systemd)"
      note "crr schtasks installs the watchdog + dashboard via Windows host tasks and has NO keep-awake task — run \`crr awake\` yourself, or enable systemd in the distro (wsl.conf) for the full set."
      run crr schtasks --install
      SERVICES_LABEL="schtasks (watchdog + dashboard; no keep-awake)"
    else
      info "Linux systemd user units"
      run crr systemd --install
      SERVICES_LABEL="systemd (watchdog + dashboard + keep-awake)"
    fi
    ;;
esac

# ---- 6. tailnet -----------------------------------------------------------
# Tailscale is how you reach the dashboard from your phone — without it the
# dashboard only answers on this machine. So (unless --no-tailscale) this step
# (a) offers to install tailscale if it's missing, (b) makes sure this machine
# is actually ON a tailnet (a one-time human sign-up), and (c) offers to serve
# the dashboard onto it.
step "5/6  Tailnet dashboard"
# The port the dashboard binds — read from crr's own effective config so the
# bootstrap never disagrees with the service it just installed. `|| true` so a
# not-yet-installed crr (dry-run) degrades to the default instead of tripping
# pipefail; the numeric check below catches anything malformed.
PORT="$(crr config --effective 2>/dev/null | awk -F' *=' '/^dashboard_port /{print $2}' | awk '{print $1}' || true)"
case "$PORT" in ''|*[!0-9]*) PORT=8377 ;; esac
TS_CONNECTED=0
TS_DONE=0
TS_IP=""

# wait_for: a blocking "the human did something OUTSIDE this terminal, then
# tell us" gate. Unlike confirm's y/N (which --yes may answer), this waits for
# a browser sign-up a script cannot do, so --yes never auto-answers it. Reads
# from /dev/tty so it still works when piped in; with no tty it cannot wait, so
# it says so and moves on rather than hanging an unattended run.
wait_for() {
  # $1 = short description of what we are waiting on
  # Probe by actually opening /dev/tty: the `[ -r /dev/tty ]` test alone gives a
  # false positive when the process has no controlling terminal (the path reads
  # as "readable" but open() fails ENXIO), so open it for real and swallow the
  # shell's open-error. With no usable tty we can't wait, so say so and move on
  # rather than hanging an unattended run.
  if ! : 2>/dev/null < /dev/tty; then
    warn "no terminal to confirm — I can't wait for you to finish this ($1). Do it manually, then re-run (or finish by hand)."
    return 1
  fi
  printf '  %s>>> when you are done, press Enter to continue <<<%s\n' "$C_WARN" "$C_R"
  local _done=""
  if ! read -r _done 2>/dev/null < /dev/tty; then
    return 1
  fi
  return 0
}

# tailscale_ensure_connected: if this machine is already on a tailnet, say so;
# otherwise direct the user to sign up + connect it, then WAIT for them to
# finish before proceeding. Returns 0 if connected, 1 otherwise.
tailscale_ensure_connected() {
  if [ "$DRY_RUN" = 1 ]; then
    note "would confirm this machine is on a tailnet (sign up at https://login.tailscale.com, then: tailscale up)"
    return 0
  fi
  local ip
  ip="$(tailscale ip -4 2>/dev/null || true)"
  if [ -n "$ip" ]; then
    ok "already on a tailnet ($ip)"
    TS_CONNECTED=1; TS_IP="$ip"
    return 0
  fi
  cat <<'EOF'
  This machine isn't on a tailnet yet. Tailscale needs a one-time sign-up:
    1. Sign up / sign in:    https://login.tailscale.com
       (create an account if you don't have one — the free plan is enough)
    2. Connect THIS machine: tailscale up        (add `sudo` on Linux if it
       complains) It prints a link — open it, sign in, and approve this device.
EOF
  if wait_for "sign up and connect this machine (the two steps above)"; then
    ip="$(tailscale ip -4 2>/dev/null || true)"
    if [ -n "$ip" ]; then
      ok "this machine is now on a tailnet ($ip)"
      TS_CONNECTED=1; TS_IP="$ip"
      return 0
    fi
    warn "I can't see this machine on a tailnet yet. Check \`tailscale status\`; once it's connected, re-run (or run \`tailscale serve --bg $PORT\` yourself)."
  fi
  return 1
}

case "$TAILSCALE" in
  0) note "skipping tailscale (--no-tailscale) — the dashboard will only be reachable on this machine" ;;
  *)
    # 1. Make sure tailscale is installed (offered, never force-installed).
    if ! have tailscale; then
      warn "tailscale not installed — it's how you reach the dashboard from your phone."
      TS_CMD="$(suggest_install tailscale)"
      [ -n "$TS_CMD" ] || TS_CMD="(install tailscale from https://tailscale.com/download)"
      if [ "$DRY_RUN" = 1 ]; then
        note "would offer to install tailscale: $TS_CMD"
      else
        printf '  suggested: %s\n' "$TS_CMD"
        if confirm "Install tailscale now?"; then
          run_sh "$TS_CMD"
          if have tailscale; then
            ok "tailscale installed"
          else
            warn "tailscale still not available after that command — install it manually:  $TS_CMD"
            note "once installed, sign up at https://login.tailscale.com if you don't have a Tailscale account yet, then connect this machine with:  tailscale up   (add sudo on Linux if it complains) — then re-run this script to finish."
          fi
        else
          warn "declined tailscale — the dashboard will only be reachable on THIS machine (http://127.0.0.1:$PORT/)."
          note "to add remote access later: install tailscale (sign up at https://login.tailscale.com if you don't have an account), then:  tailscale up   &&   tailscale serve --bg $PORT"
        fi
      fi
    fi
    # 2. Get this machine onto a tailnet (one-time human sign-up), then
    # 3. offer to serve the dashboard onto it.
    if have tailscale; then
      if tailscale_ensure_connected; then
        # Explain, then gate the actual network-exposure step on an explicit
        # yes — it is the one action that changes what other machines can
        # reach, so it never runs silently.
        cat <<EOF
  What this does:  tailscale serve --bg $PORT
  Publishes the loopback-only crr dashboard onto your Tailscale tailnet, so
  you can open it from your phone or another device on the same tailnet
  (e.g. https://${TS_IP:-<this-machine>}/) — WITHOUT it the dashboard answers
  only on this machine (http://127.0.0.1:$PORT/). It is tailnet-only: it does
  NOT expose the dashboard to the public internet, and it does not change what
  crr does.
EOF
        if confirm "Expose the dashboard on your tailnet now?"; then
          if run tailscale serve --bg "$PORT"; then
            TS_DONE=1
            TS_IP="${TS_IP:-$(tailscale ip -4 2>/dev/null || true)}"
            if [ -n "$TS_IP" ]; then
              ok "dashboard reachable at https://$TS_IP/ from devices on your tailnet"
            else
              ok "tailnet serve active (check the URL with: tailscale status)"
            fi
          else
            warn "tailscale serve failed — the dashboard remains loopback-only. Try: tailscale serve --bg $PORT"
          fi
        else
          note "on the tailnet, but you chose not to serve it now. Run later:  tailscale serve --bg $PORT"
        fi
      fi
    fi
    ;;
esac

# ---- 7. doctor + summary --------------------------------------------------
step "6/6  Health check (crr doctor)"
if [ "$DRY_RUN" = 1 ]; then
  note "would run: crr doctor"
else
  crr doctor
fi

step "Summary"
printf '  %scrr:%s %s\n' "$C_OK" "$C_R" "$(crr --version 2>/dev/null || echo unknown)"
printf '  %sshim:%s %s (%s)\n' "$C_OK" "$C_R" "$RC_FILE" "$SHELL_NAME"
printf '  %sservices:%s %s\n' "$C_OK" "$C_R" "${SERVICES_LABEL:-none}"
if [ "$TS_DONE" = 1 ]; then
  printf '  %sdashboard:%s loopback http://127.0.0.1:%s/  +  tailnet https://%s/\n' "$C_OK" "$C_R" "$PORT" "${TS_IP:-<this-machine>}"
elif [ "$TS_CONNECTED" = 1 ]; then
  printf '  %sdashboard:%s loopback http://127.0.0.1:%s/  (on the tailnet — not yet served; run: tailscale serve --bg %s)\n' "$C_OK" "$C_R" "$PORT" "$PORT"
else
  printf '  %sdashboard:%s loopback-only http://127.0.0.1:%s/\n' "$C_OK" "$C_R" "$PORT"
fi
printf '\n  %sNext steps%s\n' "$C_INFO" "$C_R"
cat <<EOF
    1. Open a NEW shell so the shim takes effect  (or: source $RC_FILE)
    2. Start claude in that shell — it is journaled automatically from then on.
    3. Check state anytime:  crr status        Dashboard:  crr web   (port $PORT)
    4. Full health check:    crr doctor

  Re-run this script any time to refresh the install (it is idempotent).
  To undo the services:  crr systemd --uninstall   (or launchd / schtasks)
EOF
exit 0
