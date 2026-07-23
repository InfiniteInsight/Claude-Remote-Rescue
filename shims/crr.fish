#!/usr/bin/env fish
# crr.fish -- Claude-Remote-Rescue shell shim for fish.
#
# Installed by `crr install-shims` (sourced from ~/.config/fish/config.fish
# via a guarded block); can also be sourced directly for
# development/testing.
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
if not set -q CRR_BIN; or test -z "$CRR_BIN"
    set -gx CRR_BIN __CRR_BIN__
end
switch "$CRR_BIN"
    case '*__CRR_BIN__*'
        set -gx CRR_BIN (command -v crr 2>/dev/null)
end

# Void call: discards all output, preserves crr's exit code, but never
# errors when crr is unusable (returns failure instead).
function _crr
    if test -z "$CRR_BIN"; or not test -x "$CRR_BIN"
        return 1
    end
    $CRR_BIN $argv >/dev/null 2>&1
end

# Output-capturing call: stdout flows through for command substitution,
# stderr is discarded so a crr-side failure never leaks text into the
# terminal. Returns failure (empty stdout) when crr is unusable.
function _crr_out
    if test -z "$CRR_BIN"; or not test -x "$CRR_BIN"
        return 1
    end
    $CRR_BIN $argv 2>/dev/null
end

# Only hook interactive shells.
if not status is-interactive
    return 0
end

function _crr_host_type
    if test -n "$TMUX"
        echo tmux
    else if test -n "$SSH_TTY"; or test -n "$SSH_CONNECTION"
        echo ssh
    else
        echo tab
    end
end

# --- register at shell startup ------------------------------------------

_crr register --pid $fish_pid --cwd $PWD --shell fish --host (_crr_host_type)

# --- last-cmd (fish_preexec) + cwd (PWD variable watch) updates ---------

function __crr_preexec --on-event fish_preexec
    _crr update $fish_pid --last-cmd $argv[1]
end

function __crr_on_pwd --on-variable PWD
    _crr update $fish_pid --cwd $PWD
end

# --- deregister on shell exit --------------------------------------------

function __crr_on_exit --on-event fish_exit
    _crr deregister $fish_pid
end

# --- claude wrapper --------------------------------------------------------

function claude
    set -l real_claude (command -s claude 2>/dev/null)
    if test -z "$real_claude"
        echo "claude: command not found" >&2
        return 127
    end

    # [lesson: flag files] clear any stale relaunch flag before this fresh
    # invocation sets up its own repair loop.
    _crr take-relaunch-flag $fish_pid >/dev/null 2>&1

    set -l crr_argv $argv

    set -l has_resume 0
    set -l has_sid 0
    set -l resume_sid ""
    set -l sid_val ""
    set -l want_resume_val 0
    set -l want_sid_val 0

    for a in $argv
        if test $want_resume_val -eq 1
            switch $a
                case '-*'
                    # bare --resume immediately followed by another flag
                case '*'
                    set resume_sid $a
            end
            set want_resume_val 0
            continue
        end
        if test $want_sid_val -eq 1
            switch $a
                case '-*'
                case '*'
                    set sid_val $a
            end
            set want_sid_val 0
            continue
        end
        switch $a
            case '--resume'
                set has_resume 1
                set want_resume_val 1
            case '--resume=*'
                set has_resume 1
                set resume_sid (string sub -s 10 -- $a)
            case '--session-id'
                set has_sid 1
                set want_sid_val 1
            case '--session-id=*'
                set has_sid 1
                set sid_val (string sub -s 14 -- $a)
        end
    end

    set -l sid ""
    set -l verified true

    if test $has_resume -eq 0; and test $has_sid -eq 0
        set sid (_crr_out new-uuid)
        if test -n "$sid"
            set -a crr_argv --session-id $sid
        end
        set verified true
    else if test $has_resume -eq 1
        if test -n "$resume_sid"
            set sid $resume_sid
            set verified true
        else
            set sid (_crr_out guess-sid $PWD)
            set verified false
        end
    else if test $has_sid -eq 1
        set sid $sid_val
        set verified true
    end

    if test -n "$sid"
        _crr update $fish_pid --claude-sid $sid --sid-verified $verified
    end

    # Sid re-verification: only needed for a guessed (unverified) sid from
    # a bare `claude --resume` (picker). Spawned in the background right
    # away so it runs concurrently with the foreground claude session;
    # crr itself sleeps out the ~10s picker window before checking.
    #
    # `crr now` (not `date +%s`) for the launch timestamp: whole-second
    # resolution is too coarse -- a picker-guess transcript written in the
    # same wall-clock second as the launch could look "newer" than a
    # truncated launch time and get spuriously verified.
    if test -n "$sid"; and test "$verified" = "false"
        set -l launched_epoch (_crr_out now)
        if test -n "$launched_epoch"
            _crr verify-sid $fish_pid --started $launched_epoch >/dev/null 2>&1 &
            disown 2>/dev/null
        end
    end

    # env-leakage lesson: claude never sees CRR_* control vars.
    set -l unset_flags
    for line in (env)
        set -l name (string split -m 1 -- '=' $line)[1]
        switch $name
            case 'CRR_*'
                set -a unset_flags -u $name
        end
    end

    set -l claude_status 0
    while true
        env $unset_flags $real_claude $crr_argv
        set claude_status $status

        if _crr take-relaunch-flag $fish_pid
            set -l resume_words (_crr_out resume-argv $fish_pid)
            if test (count $resume_words) -gt 0
                set crr_argv $resume_words
                continue
            end
        end
        break
    end
    return $claude_status
end
