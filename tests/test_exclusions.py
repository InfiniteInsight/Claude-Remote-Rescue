"""Dashboard-managed discovery exclusions (admin section).

config.toml's ``discover_exclude_dirs`` stays the user's hand-owned
baseline; these are the additions the dashboard can write. Two stores, one
effective list — the web never rewrites the user's TOML (stdlib has no TOML
writer, and a generated file would lose their comments).
"""

import pytest

from crr.core import exclusions


def test_normalize_strips_blanks_and_dedupes_preserving_order():
    assert exclusions.normalize(["  .claude-mem ", "", "   ", "foo", "foo"]) == [".claude-mem", "foo"]


def test_normalize_rejects_non_list_or_non_string_entries():
    for bad in ("not-a-list", 5, None, [1, 2], [None], [["nested"]]):
        with pytest.raises(exclusions.ExclusionError):
            exclusions.normalize(bad)


def test_normalize_caps_count_and_entry_length():
    with pytest.raises(exclusions.ExclusionError):
        exclusions.normalize(["d%d" % i for i in range(exclusions.MAX_ENTRIES + 1)])
    with pytest.raises(exclusions.ExclusionError):
        exclusions.normalize(["x" * (exclusions.MAX_ENTRY_LEN + 1)])


def test_store_roundtrip_and_absent_file_is_empty(tmp_path):
    store = exclusions.ExclusionStore(tmp_path)
    assert store.read() == []           # absent file -> honest empty
    store.write(["  .claude-mem", "scratch "])
    assert store.read() == [".claude-mem", "scratch"]
    assert exclusions.ExclusionStore(tmp_path).read() == [".claude-mem", "scratch"]


def test_store_unreadable_file_degrades_to_empty(tmp_path):
    (tmp_path / "exclusions.json").write_text("{not json", encoding="utf-8")
    # A corrupt file must not break discovery — it degrades to no managed
    # exclusions rather than raising on every panel open.
    assert exclusions.ExclusionStore(tmp_path).read() == []


def test_effective_merges_config_baseline_with_managed(tmp_path):
    store = exclusions.ExclusionStore(tmp_path)
    store.write(["scratch"])
    assert exclusions.effective([".claude-mem"], store.read()) == [".claude-mem", "scratch"]
    # a managed duplicate of the config value is not listed twice
    store.write([".claude-mem", "other"])
    assert exclusions.effective([".claude-mem"], store.read()) == [".claude-mem", "other"]
