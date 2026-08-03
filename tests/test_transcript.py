"""Last-prompt extractor tests (pure core, synthetic records).

The skip-list is empirical — it drops the real garbage that showed up on
real cards: tool-result turns, slash-command wrappers, local-command
output, task-notifications, system-reminders, caveats, interrupt markers,
compaction continuations, and meta lines. These tests encode each with a
synthetic record so no real conversation content is needed.
"""

import pytest

from crr.core import transcript


def _user(content, **extra):
    rec = {"type": "user", "message": {"role": "user", "content": content}}
    rec.update(extra)
    return rec


def _assistant(text, model=None):
    msg = {"role": "assistant", "content": text}
    if model is not None:
        msg["model"] = model
    return {"type": "assistant", "message": msg}


def test_returns_last_real_user_prompt():
    records = [
        _user("first question"),
        _assistant("an answer"),
        _user("the most recent real prompt"),
    ]
    assert transcript.last_prompt(records, cap=100) == "the most recent real prompt"


def test_skips_assistant_turns():
    records = [_user("real prompt"), _assistant("blah"), _assistant("more")]
    assert transcript.last_prompt(records, cap=100) == "real prompt"


def test_skips_tool_result_turns():
    # user content that is a list of tool_result blocks is not a human prompt.
    records = [
        _user("real prompt"),
        _user([{"type": "tool_result", "content": "output"}]),
    ]
    assert transcript.last_prompt(records, cap=100) == "real prompt"


def test_uses_text_blocks_in_list_content():
    records = [_user([{"type": "text", "text": "prompt with an image"},
                      {"type": "image", "source": {}}])]
    assert transcript.last_prompt(records, cap=100) == "prompt with an image"


def test_skips_meta_lines():
    records = [_user("real prompt"), _user("<command-name>/foo", isMeta=True)]
    assert transcript.last_prompt(records, cap=100) == "real prompt"


import pytest


@pytest.mark.parametrize("noise", [
    "<command-name>/export</command-name><command-message>export</command-message>",
    "<local-command-stdout>done</local-command-stdout>",
    "<task-notification>a background task finished</task-notification>",
    "<user-prompt-submit-hook>hook output</user-prompt-submit-hook>",
    "Caveat: The messages below were generated while running local commands.",
    "This session is being continued from a previous conversation that ran out of context.",
    "[Request interrupted by user]",
])
def test_skips_known_noise_wrappers(noise):
    records = [_user("real prompt"), _user(noise)]
    assert transcript.last_prompt(records, cap=100) == "real prompt"


def test_strips_trailing_system_reminder_but_keeps_the_prompt():
    records = [_user("do the thing\n<system-reminder>injected note</system-reminder>")]
    assert transcript.last_prompt(records, cap=100) == "do the thing"


def test_collapses_whitespace_and_caps_length():
    records = [_user("a   prompt\nwith\n\nlots   of space " + "x" * 100)]
    out = transcript.last_prompt(records, cap=20)
    assert out == ("a prompt with lots o")[:20]
    assert len(out) == 20


def test_empty_when_no_real_prompt():
    records = [_assistant("only assistant"), _user([{"type": "tool_result", "content": "x"}])]
    assert transcript.last_prompt(records, cap=100) == ""


# --- model extraction (task #13) -----------------------------------------

def test_extract_model_reads_assistant_model():
    assert transcript.extract_model(_assistant("hi", model="claude-opus-4-8")) == "claude-opus-4-8"


def test_extract_model_ignores_non_assistant_turns():
    assert transcript.extract_model(_user("a prompt")) is None


def test_extract_model_skips_synthetic_turns():
    # Reading backward, API-error / interrupt turns (model "<synthetic>")
    # cluster at the tail; they must not be stamped on a card as the model.
    assert transcript.extract_model(_assistant("interrupted", model="<synthetic>")) is None


def test_extract_model_none_when_model_absent():
    assert transcript.extract_model(_assistant("no model field")) is None


def test_tail_facts_returns_last_prompt_and_last_model():
    records = [
        _user("older prompt"),
        _assistant("answer", model="claude-opus-4-8"),
        _user("the most recent prompt"),
        _assistant("later answer", model="claude-opus-5"),
    ]
    facts = transcript.tail_facts(records, cap=100)
    assert facts == {
        "last_prompt": "the most recent prompt",
        "model": "claude-opus-5",
        "last_active": "",
    }


def test_tail_facts_skips_synthetic_to_find_the_real_model():
    records = [
        _user("prompt"),
        _assistant("real answer", model="claude-opus-4-8"),
        _assistant("interrupted", model="<synthetic>"),  # most recent, but noise
    ]
    assert transcript.tail_facts(records, cap=100)["model"] == "claude-opus-4-8"


def test_tail_facts_honest_empties_when_absent():
    facts = transcript.tail_facts([_user([{"type": "tool_result", "content": "x"}])], cap=100)
    assert facts == {"last_prompt": "", "model": "", "last_active": ""}


def test_tail_facts_returns_last_active_from_the_newest_timestamped_record():
    # last_active is the ISO timestamp of the newest record that has one —
    # independent of which record supplies the prompt/model.
    records = [
        _user("older prompt", timestamp="2026-01-01T00:00:00Z"),
        _assistant("answer", model="claude-opus-4-8"),
        _user("the most recent prompt", timestamp="2026-01-02T00:00:00Z"),
    ]
    facts = transcript.tail_facts(records, cap=100)
    assert facts["last_active"] == "2026-01-02T00:00:00Z"


def test_tail_facts_last_active_skips_records_with_no_timestamp():
    # The newest record has no timestamp; the walk keeps going backward
    # until it finds one that does.
    records = [
        _user("older prompt", timestamp="2026-01-01T00:00:00Z"),
        _user("newest prompt but untimestamped"),
    ]
    facts = transcript.tail_facts(records, cap=100)
    assert facts["last_active"] == "2026-01-01T00:00:00Z"


# --- cwd extraction (T-C — discovery's authoritative cwd source) ----------


def test_extract_cwd_reads_the_stamped_cwd():
    record = _user("a prompt", cwd="/home/u/proj")
    assert transcript.extract_cwd(record) == "/home/u/proj"


def test_extract_cwd_none_when_absent():
    assert transcript.extract_cwd(_user("a prompt")) is None


def test_extract_cwd_none_for_non_mapping():
    assert transcript.extract_cwd("not a record") is None


@pytest.mark.parametrize("bad", ["", 123, None])
def test_extract_cwd_none_for_malformed_values(bad):
    assert transcript.extract_cwd({"cwd": bad}) is None


# --- search (F1 — `crr recall`) -------------------------------------------


def test_search_matches_a_user_prompt_case_insensitively():
    records = [_user("the quick brown Fox")]
    out = transcript.search(records, "FOX", cap=100)
    assert out == [{"role": "user", "text": "the quick brown Fox", "index": 0, "timestamp": ""}]


def test_search_matches_assistant_text():
    records = [_assistant("the answer involves a fox")]
    out = transcript.search(records, "fox", cap=100)
    assert out == [
        {"role": "assistant", "text": "the answer involves a fox", "index": 0, "timestamp": ""}
    ]


def test_search_no_match_returns_empty_list():
    records = [_user("hello there"), _assistant("general kenobi")]
    assert transcript.search(records, "nonexistent", cap=100) == []


def test_search_has_no_context_param():
    # `context` used to be accepted but silently no-op for any non-zero
    # value — an invisible lie about a capability (-C snippet widening)
    # that isn't wired in this slice. Dropped entirely rather than kept as
    # a no-op knob; a TypeError here is the guard against it creeping back.
    records = [_user("a fox sighting")]
    with pytest.raises(TypeError):
        transcript.search(records, "fox", cap=100, context=2)


def test_search_preserves_chronological_index_across_matches():
    records = [
        _user("first fox sighting"),
        _assistant("no fox here"),
        _user("second fox sighting"),
    ]
    out = transcript.search(records, "fox", cap=100)
    assert [m["index"] for m in out] == [0, 1, 2]
    assert [m["role"] for m in out] == ["user", "assistant", "user"]


def test_search_skips_tool_result_and_meta_noise():
    records = [
        _user([{"type": "tool_result", "content": "fox output"}]),
        _user("<task-notification>a fox task finished</task-notification>"),
        _user("<command-name>/fox", isMeta=True),
        _user("a real fox prompt"),
    ]
    out = transcript.search(records, "fox", cap=100)
    assert out == [
        {"role": "user", "text": "a real fox prompt", "index": 3, "timestamp": ""}
    ]


def test_search_ignores_assistant_tool_use_turns_with_no_text():
    records = [{"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "fox_tool", "input": {}}],
    }}]
    assert transcript.search(records, "fox", cap=100) == []


def test_search_caps_and_cleans_the_matched_text():
    raw = "a   fox\nwith\n\nlots   of space " + "x" * 100
    records = [_user(raw)]
    out = transcript.search(records, "fox", cap=20)
    assert out[0]["text"] == " ".join(raw.split())[:20]
    assert len(out[0]["text"]) == 20


def test_search_includes_timestamp_when_present():
    records = [_user("a fox prompt", timestamp="2026-01-02T00:00:00Z")]
    out = transcript.search(records, "fox", cap=100)
    assert out[0]["timestamp"] == "2026-01-02T00:00:00Z"


def test_search_skips_synthetic_assistant_turns():
    # <synthetic> assistant turns are the API-error/interrupt records
    # Claude Code writes — noise, not real conversation (mirrors
    # extract_model's skip). They must not surface in recall.
    records = [_assistant("a fox error occurred", model="<synthetic>")]
    assert transcript.search(records, "fox", cap=100) == []


def test_search_matches_assistant_text_from_list_content_blocks():
    records = [{"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "text", "text": "a fox in a list block"}],
    }}]
    out = transcript.search(records, "fox", cap=100)
    assert out[0]["text"] == "a fox in a list block"


# --- turn_boundary (adopt --takeover) -------------------------------------
#
# Classify a single record as a turn boundary: "assistant-end" (the only
# safe-to-take-over tail — the response is finished and Claude Code is
# waiting on the user), "mid-turn" (a response or tool round-trip is still
# in flight), "user-prompt" (a real prompt awaiting a reply), or "other"
# (non-turn records: permission-mode/pr-link/bridge-session, isMeta, or
# malformed input). Shapes below mirror real Claude Code JSONL records.


def test_turn_boundary_assistant_end_turn_is_assistant_end():
    record = {"type": "assistant", "message": {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "done"}],
    }}
    assert transcript.turn_boundary(record) == "assistant-end"


def test_turn_boundary_assistant_tool_use_with_text_only_content_is_mid_turn():
    # Empirical: Claude Code stamps stop_reason "tool_use" even when the
    # record's only content block is text/thinking (the tool_use block
    # itself lives on a later record) — so this text-only record is still
    # mid-turn, not a safe boundary.
    record = {"type": "assistant", "message": {
        "role": "assistant",
        "model": "claude-opus-4-8",
        "stop_reason": "tool_use",
        "content": [{"type": "text", "text": "let me check that"}],
    }}
    assert transcript.turn_boundary(record) == "mid-turn"


def test_turn_boundary_real_user_prompt_is_user_prompt():
    record = {"type": "user", "message": {"role": "user", "content": "fix the bug"}}
    assert transcript.turn_boundary(record) == "user-prompt"


def test_turn_boundary_user_tool_result_with_tooluseresult_key_is_mid_turn():
    record = {
        "type": "user",
        "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "output"},
        ]},
        "toolUseResult": {"stdout": "output"},
    }
    assert transcript.turn_boundary(record) == "mid-turn"


def test_turn_boundary_synthetic_assistant_is_other():
    # <synthetic> assistant records (API errors/interrupts) carry no real
    # turn — treated transparently, like extract_model/_assistant_text
    # already do, so a caller scanning backward for the newest non-"other"
    # record skips straight past it to the real prior turn instead of
    # getting stuck reporting "mid-turn" forever.
    record = {"type": "assistant", "message": {
        "role": "assistant",
        "model": "<synthetic>",
        "stop_reason": "end_turn",
        "content": "interrupted",
    }}
    assert transcript.turn_boundary(record) == "other"


def test_turn_boundary_synthetic_assistant_with_non_end_turn_stop_reason_is_also_other():
    # The transparency rule applies regardless of stop_reason — synthetic
    # is never a real turn, so it's never mid-turn either.
    record = {"type": "assistant", "message": {
        "role": "assistant",
        "model": "<synthetic>",
        "stop_reason": "stop_sequence",
        "content": "interrupted",
    }}
    assert transcript.turn_boundary(record) == "other"


@pytest.mark.parametrize("rtype", ["permission-mode", "pr-link", "bridge-session"])
def test_turn_boundary_non_turn_types_are_other(rtype):
    assert transcript.turn_boundary({"type": rtype}) == "other"


def test_turn_boundary_meta_user_record_is_other():
    record = {"type": "user", "isMeta": True,
              "message": {"role": "user", "content": "<command-name>/foo"}}
    assert transcript.turn_boundary(record) == "other"


@pytest.mark.parametrize("bad", [None, [], "not a record", 42])
def test_turn_boundary_non_mapping_input_is_other(bad):
    assert transcript.turn_boundary(bad) == "other"
