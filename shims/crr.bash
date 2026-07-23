#!/usr/bin/env bash
# crr.bash -- Claude-Remote-Rescue shell shim for bash.
#
# Installed by `crr install-shims` (sourced from ~/.bashrc via a guarded
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
case "$-" in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

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

_crr register --pid "$$" --cwd "$PWD" --shell bash --host "$(_crr_host_type)"

# --- last-cmd (DEBUG trap) + cwd (PROMPT_COMMAND) updates ---------------
#
# The DEBUG trap fires before every simple command, including ones nested
# deep inside a single typed line; __crr_bash_preexec_ran gates it to fire
# (and journal) only once per prompt cycle, reset by PROMPT_COMMAND after
# the command finishes -- the standard minimal bash-preexec pattern.

__crr_bash_preexec_ran=""

__crr_debug_trap() {
  [ -n "$__crr_bash_preexec_ran" ] && return
  case "$BASH_COMMAND" in
    __crr_prompt_command*|__crr_debug_trap*) return ;;
  esac
  __crr_bash_preexec_ran=1
  local cmd
  cmd=$(HISTTIMEFORMAT= builtin history 1 2>/dev/null | sed -e 's/^[ ]*[0-9]*[ ]*//')
  [ -n "$cmd" ] && _crr update "$$" --last-cmd "$cmd"
}
trap '__crr_debug_trap' DEBUG

__crr_prompt_command() {
  local ec=$?
  __crr_bash_preexec_ran=""
  _crr update "$$" --cwd "$PWD"
  return $ec
}
PROMPT_COMMAND="__crr_prompt_command${PROMPT_COMMAND:+;${PROMPT_COMMAND}}"

# --- deregister on shell exit --------------------------------------------

__crr_exit_handler() {
  _crr deregister "$$"
}
trap '__crr_exit_handler' EXIT

# --- claude() wrapper -----------------------------------------------------

claude() {
  local real_claude
  real_claude="$(type -P claude 2>/dev/null)"
  if [ -z "$real_claude" ]; then
    echo "claude: command not found" >&2
    return 127
  fi

  # [lesson: flag files] clear any stale relaunch flag before this fresh
  # invocation sets up its own repair loop.
  _crr take-relaunch-flag "$$" >/dev/null 2>&1

  local -a argv
  argv=("$@")

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
      argv+=(--session-id "$sid")
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
    env "${unset_flags[@]}" "$real_claude" "${argv[@]}"
    claude_status=$?

    if _crr take-relaunch-flag "$$"; then
      local -a resume_words=()
      local w
      while IFS= read -r w; do
        resume_words+=("$w")
      done < <(_crr_out resume-argv "$$")
      if [ ${#resume_words[@]} -gt 0 ]; then
        argv=("${resume_words[@]}")
        continue
      fi
    fi
    break
  done
  return $claude_status
}
