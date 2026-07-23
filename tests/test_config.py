import sys

from crr import config


def write_config(state, text):
    """config.toml lives in the state-dir parent."""
    path = state.parent / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_path_is_state_dir_parent(crr_state):
    assert config.config_path() == crr_state.parent / "config.toml"


def test_missing_file_yields_defaults(crr_state):
    cfg = config.load_config()
    assert cfg == {
        "web_port": 8377,
        "host_allowlist": [],
        "archive_retention_days": 30,
    }


def test_valid_toml_parsed(crr_state):
    write_config(
        crr_state,
        'web_port = 9000\n'
        'host_allowlist = ["mybox.lan", "phone.example"]\n'
        'archive_retention_days = 7\n',
    )
    cfg = config.load_config()
    assert cfg["web_port"] == 9000
    assert cfg["host_allowlist"] == ["mybox.lan", "phone.example"]
    assert cfg["archive_retention_days"] == 7


def test_partial_and_bad_types_fall_back_per_key(crr_state):
    write_config(
        crr_state,
        'web_port = "not a port"\n'
        'archive_retention_days = 14\n'
        'host_allowlist = "not-a-list"\n',
    )
    cfg = config.load_config()
    assert cfg["web_port"] == 8377  # bad type ignored
    assert cfg["host_allowlist"] == []
    assert cfg["archive_retention_days"] == 14


def test_malformed_toml_warns_and_defaults(crr_state, capsys):
    write_config(crr_state, "this is [ not toml =")
    cfg = config.load_config()
    assert cfg["web_port"] == 8377
    assert "warning" in capsys.readouterr().err


def test_pre_311_missing_tomllib_degrades_with_warning(crr_state, capsys, monkeypatch):
    """On Python < 3.11 tomllib is absent: file ignored, defaults, stderr note."""
    write_config(crr_state, "web_port = 9000\n")
    monkeypatch.setattr(config, "tomllib", None)
    cfg = config.load_config()
    assert cfg["web_port"] == 8377
    err = capsys.readouterr().err
    assert "tomllib" in err and "warning" in err


def test_tomllib_present_on_modern_python():
    if sys.version_info >= (3, 11):
        assert config.tomllib is not None
