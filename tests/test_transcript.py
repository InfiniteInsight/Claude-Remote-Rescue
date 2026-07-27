"""Last-prompt extractor tests (pure core, synthetic records).

The skip-list is empirical — it drops the real garbage that showed up on
real cards: tool-result turns, slash-command wrappers, local-command
output, task-notifications, system-reminders, caveats, interrupt markers,
compaction continuations, and meta lines. These tests encode each with a
synthetic record so no real conversation content is needed.
"""

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
    assert facts == {"last_prompt": "the most recent prompt", "model": "claude-opus-5"}


def test_tail_facts_skips_synthetic_to_find_the_real_model():
    records = [
        _user("prompt"),
        _assistant("real answer", model="claude-opus-4-8"),
        _assistant("interrupted", model="<synthetic>"),  # most recent, but noise
    ]
    assert transcript.tail_facts(records, cap=100)["model"] == "claude-opus-4-8"


def test_tail_facts_honest_empties_when_absent():
    facts = transcript.tail_facts([_user([{"type": "tool_result", "content": "x"}])], cap=100)
    assert facts == {"last_prompt": "", "model": ""}
