#!/usr/bin/env zsh
# crr.zsh -- Claude-Remote-Rescue shell shim for zsh.
#
# Installed by `crr install-shims` (sourced from ~/.zshrc via a guarded
# block); can also be sourced directly for development/testing.
#
# See DESIGN.md for the lessons this file encodes:
#   - [lesson: PATH poisoning] crr is invoked by absolute path only
#     (CRR_BIN, baked in at install time); every hook below is a total,
#     silent no-op when that binary is missing or not executable.
#   - [lesson: env leakage] claude never sees CRR_* control vars.
#   - [lesson: flag files] the claude() wrapper clears any stale kick
#     relaunch flag at its own start, and only relaunches when the flag
#     really was set by a landed kick.

# --- CRR_BIN: absolute path to crr, rewritten by `crr install-shims`. ---
# A pre-set CRR_BIN in the environment (tests, manual override) wins.
: "${CRR_BIN:=__CRR_BIN__}"
case "$CRR_BIN" in
  *__CRR_BIN__*) CRR_BIN="$(command -v crr 2>/dev/null)" ;;
esac

# Void call: discards all output, preserves crr's exit code, but never
# errors when crr is unusable (returns failure instead).
_crr() {
  if [ -z "$CRR_BIN" ] || [ ! -x "$CRR_BIN" ]; then
    return 1
  fi
  "$CRR_BIN" "$@" >/dev/null 2>&1
}

# Output-capturing call: stdout flows through for command substitution,
# stderr is discarded so a crr-side failure never leaks text into the
# terminal. Returns failure (empty stdout) when crr is unusable.
_crr_out() {
  if [ -z "$CRR_BIN" ] || [ ! -x "$CRR_BIN" ]; then
    return 1
  fi
  "$CRR_BIN" "$@" 2>/dev/null
}

# Only hook interactive shells.
[[ -o interactive ]] || return 0

_crr_host_type() {
  if [ -n "$TMUX" ]; then
    printf '%s' tmux
  elif [ -n "$SSH_TTY" ] || [ -n "$SSH_CONNECTION" ]; then
    printf '%s' ssh
  else
    printf '%s' tab
  fi
}

# --- register at shell startup ------------------------------------------

_crr register --pid "$$" --cwd "$PWD" --shell zsh --host "$(_crr_host_type)"

# --- last-cmd (preexec) + cwd (chpwd) updates ----------------------------

autoload -Uz add-zsh-hook

_crr_preexec() {
  # $1 is the command about to run, exactly as typed (zsh hands it to us
  # directly -- no history-parsing tricks needed like bash).
  _crr update "$$" --last-cmd "$1"
}
add-zsh-hook preexec _crr_preexec

_crr_chpwd() {
  _crr update "$$" --cwd "$PWD"
}
add-zsh-hook chpwd _crr_chpwd

# --- deregister on shell exit --------------------------------------------

_crr_zshexit() {
  _crr deregister "$$"
}
add-zsh-hook zshexit _crr_zshexit

# --- claude() wrapper -----------------------------------------------------

claude() {
  local real_claude
  real_claude="$(whence -p claude 2>/dev/null)"
  if [ -z "$real_claude" ]; then
    echo "claude: command not found" >&2
    return 127
  fi

  # [lesson: flag files] clear any stale relaunch flag before this fresh
  # invocation sets up its own repair loop.
  _crr take-relaunch-flag "$$" >/dev/null 2>&1

  # [zsh gotcha] `argv` is a special alias for the positional parameters
  # ($@) in zsh -- merely declaring `local -a argv` clobbers $@ before
  # any assignment happens. Use a differently-named array throughout.
  local -a crr_argv
  crr_argv=("$@")

  local has_resume=0 has_sid=0 resume_sid="" sid_val=""
  local want_resume_val=0 want_sid_val=0
  local a
  for a in "$@"; do
    if [ "$want_resume_val" = 1 ]; then
      case "$a" in
        -*) : ;;
        *) resume_sid="$a" ;;
      esac
      want_resume_val=0
      continue
    fi
    if [ "$want_sid_val" = 1 ]; then
      case "$a" in
        -*) : ;;
        *) sid_val="$a" ;;
      esac
      want_sid_val=0
      continue
    fi
    case "$a" in
      --resume) has_resume=1; want_resume_val=1 ;;
      --resume=*) has_resume=1; resume_sid="${a#--resume=}" ;;
      --session-id) has_sid=1; want_sid_val=1 ;;
      --session-id=*) has_sid=1; sid_val="${a#--session-id=}" ;;
    esac
  done

  local sid="" verified="true"
  if [ "$has_resume" = 0 ] && [ "$has_sid" = 0 ]; then
    sid="$(_crr_out new-uuid)"
    if [ -n "$sid" ]; then
      crr_argv+=(--session-id "$sid")
    fi
    verified="true"
  elif [ "$has_resume" = 1 ]; then
    if [ -n "$resume_sid" ]; then
      sid="$resume_sid"
      verified="true"
    else
      sid="$(_crr_out guess-sid "$PWD")"
      verified="false"
    fi
  elif [ "$has_sid" = 1 ]; then
    sid="$sid_val"
    verified="true"
  fi

  if [ -n "$sid" ]; then
    _crr update "$$" --claude-sid "$sid" --sid-verified "$verified"
  fi

  # Sid re-verification: only needed for a guessed (unverified) sid from
  # a bare `claude --resume` (picker). Spawned in the background right
  # away so it runs concurrently with the foreground claude session;
  # crr itself sleeps out the ~10s picker window before checking.
  #
  # `crr now` (not `date +%s`) for the launch timestamp: whole-second
  # resolution is too coarse -- a picker-guess transcript written in the
  # same wall-clock second as the launch could look "newer" than a
  # truncated launch time and get spuriously verified.
  if [ -n "$sid" ] && [ "$verified" = "false" ]; then
    local launched_epoch
    launched_epoch="$(_crr_out now)"
    if [ -n "$launched_epoch" ]; then
      ( _crr verify-sid "$$" --started "$launched_epoch" & ) >/dev/null 2>&1
    fi
  fi

  # env-leakage lesson: claude never sees CRR_* control vars.
  local -a unset_flags=()
  local __name
  while IFS='=' read -r __name _; do
    case "$__name" in
      CRR_*) unset_flags+=(-u "$__name") ;;
    esac
  done < <(env)

  local claude_status=0
  while :; do
    env "${unset_flags[@]}" "$real_claude" "${crr_argv[@]}"
    claude_status=$?

    if _crr take-relaunch-flag "$$"; then
      local -a resume_words=()
      local w
      while IFS= read -r w; do
        resume_words+=("$w")
      done < <(_crr_out resume-argv "$$")
      if [ ${#resume_words[@]} -gt 0 ]; then
        crr_argv=("${resume_words[@]}")
        continue
      fi
    fi
    break
  done
  return $claude_status
}
