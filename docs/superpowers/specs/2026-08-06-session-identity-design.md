# Spec — Session identity: matching a Claude mobile conversation to a crr card

Status: approved (design agreed with user 2026-08-06).

## Problem

You are looking at a conversation in the Claude mobile app and cannot tell
which crr dashboard card it is. The mobile list shows a **title**, a
connected/disconnected state, a relative time, and a last-message preview —
but **no session id and no working directory**. crr's cards show pid, sid8,
cwd, and the last prompt/reply. The two views share no obvious key, so
finding "the session I was just in" means guessing.

## Key finding (empirical, 2026-08-06)

Claude Code **already writes the mobile title into the transcript**. No
injection, no marker, no new convention is needed — crr simply isn't reading
it:

| mobile list shows | transcript record |
| --- | --- |
| "Learn how LLMs and AI work from ba…" | `{"type":"ai-title","aiTitle":"Learn how LLMs and AI work from basics",...}` |
| "Install CUDA paths and reboot Lovelace" | same shape |

Every session also carries a memorable `slug` (`majestic-zooming-wren`,
`wiggly-snuggling-catmull`) on ordinary records. The slug is NOT what the
mobile app displays — the AI-chosen title is — so the slug is a fallback
label only.

Measured on the 14 most recent real transcripts (`.claude-mem` excluded):

- newest `ai-title` sits at most **39 lines** from the tail
- newest `slug` sits at most **18 lines** from the tail
- both are inside the existing `model_tail_lines` (200) window, so **no new
  config knob is required**
- 3 of 14 had no `ai-title`; 6 of 14 had no `slug`; 2 had neither

## The constraint that shapes everything

**Nothing can run inside a Disconnected session.** Most sessions in the
user's mobile list are disconnected, and that is precisely when
identification is needed. A command or a hook only works while connected.
Only data already written to the transcript survives disconnection —
which is why `aiTitle` is the load-bearing piece and the command/hook are
convenience on top.

## Design

### 1. Card carries the title (primary identifier)

`transcript.tail_facts` / `transcript_source.read_tail_facts` pick up two
more fields on the SAME backward walk they already do (no extra file read,
no new window):

- `title` — the newest `ai-title` record's `aiTitle`
- `slug` — the newest record carrying a non-empty `slug`

Both honest `""` when absent. Contract: `SESSION_CARD_KEYS` gains `title`
and `slug`; `SESSIONS_CONTRACT_VERSION` 5 → 6.

Dashboard: the title renders as the card's headline, so it matches the
phone's list verbatim. When there is no title, the slug is shown instead;
when there is neither, nothing is shown — never a fabricated name.

### 2. `crr whoami`

Walks up the process tree from its own pid to the nearest journaled shell
(verified: 3 hops from a Bash tool call — `python -> bash -> claude
--resume <sid> -> fish`, the fish being the journaled entry). Prints that
session's title, slug, sid8, pid, cwd and classifier state.

Asked from Claude mobile ("run crr whoami"), the answer lands in the
conversation. Refuses honestly when no journaled ancestor is found (e.g.
run from a shell crr does not track) rather than guessing.

### 3. Automatic identity via a SessionStart hook

`crr hook session-start` prints the same one-line identity. Claude Code's
`SessionStart` hook injects it into the session context at start/resume, so
Claude always knows which crr session it is and can answer instantly with
no command.

Honest limitation to state in the docs: hook output is **context, not a
rendered message** — it does not put a visible label in the mobile list. It
makes asking instant; it does not make identity passive. And like `whoami`,
it cannot help a disconnected session.

Installation is printed for the user to add to `settings.json` (crr already
declines to rewrite user config files it does not own; `settings.json` is
JSON so a future `--install` is feasible, but is out of scope here).

### 4. Animated `searching…`

The recall panel shows static `searching…` text. With full-corpus search
now taking ~0.7s, it needs a visible progress affordance: an animated
ellipsis/pulse while the request is in flight, honoring
`prefers-reduced-motion`.

## Risks accepted

- `ai-title` and `slug` are **undocumented internal Claude Code format** and
  may change without notice. crr already depends on this file's shape for
  prompts, models and timestamps, so this is the same class of dependency —
  but both fields must degrade to `""`, never raise.
- The title **evolves** during a conversation (one session had 559
  `ai-title` records). The newest wins, which is what the mobile app shows.
- Not every session has a title or a slug; the UI must have an honest
  fallback rather than inventing an identifier.

## Non-goals

- Making the title searchable via the recall box (titles are not
  conversation turns; the box searches what was said). Eyeballing the
  headline is the intended match path.
- Writing to `settings.json` automatically.
- Any attempt to modify or inject into the Claude mobile UI — crr cannot,
  and should not pretend to.
