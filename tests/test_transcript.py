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


def _assistant(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": text}}


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
