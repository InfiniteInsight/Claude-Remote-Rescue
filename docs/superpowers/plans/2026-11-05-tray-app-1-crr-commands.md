# Tray app, part 1: the crr commands it drives

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give crr the CLI surface the Windows tray app needs (settings, service control, logs and the phone QR) as versioned JSON the app can read over `wsl.exe`.

**Architecture:**
- **New commands in `crr/cli.py`:**
  - `crr config set` and `crr config unset`
  - `crr config --effective --json`
  - `crr service status|start|stop`
  - `crr logs`
  - `crr qr --json`
- **Settings changes go into a new crr-owned overlay store,** `config_overrides.json`, layered over the hand-owned `config.toml`. Five keys keep using the dashboard's existing stores.
- **Platform work lives in the adapters.** The systemd and journald argv come from `crr/adapters/systemd.py`.
- **Payload shapes and rules are pure core:** `crr/core/config.py`, `crr/core/config_overrides.py`, `crr/core/service.py` and the contracts in `crr/core/contracts.py`.

**Tech Stack:** Python 3.11+ stdlib only, pytest, import-linter.

**Spec:** `docs/superpowers/specs/2026-11-05-tray-app-design.md` ("Changes to crr", items 1 to 4). Items 5 and 6, tab handoff and the dashboard additions, have their own plans.

## Global Constraints

- **No runtime dependencies.** `pyproject.toml` dependencies don't change.
- **Layering:** `crr.cli` → `crr.adapters` → `crr.core`. Core imports neither. `lint-imports` must stay green.
- **Test-first.** Watch each new test fail before implementing it.
- **Every JSON shape a command prints is a versioned contract.** That means:
  - a `*_CONTRACT_VERSION = 1` constant in `crr/core/contracts.py`, with a `# v1 …` ledger comment directly above it
  - a `validate_*` function
  - a `_assert_contiguous` line in `tests/test_version_ledger.py`
  - a mention in AGENTS.md's Contracts line

  The command validates its own payload before printing it, as `crr status --json` does.
- **New judgment-call constants go in `crr/core/config.py` DEFAULTS.** Each one bumps `CONFIG_DEFAULTS_VERSION` and gets a ledger entry.
- **`config.toml` is never written by crr.** It's the user's hand-owned file (`crr/core/exclusions.py` records why).
- **`crr service stop` is a runtime stop only.** It never calls `systemctl disable`. A WSL restart, a reboot or `crr service start` brings crr back.
- **Output conventions:**
  - JSON goes to stdout. Errors and notes go to stderr.
  - Exit codes: 0 for success, 1 for refused or failed, 2 for usage, an invalid value, or an unsupported platform. These match existing commands.
- **Running tests in a worktree:** set `PYTHONPATH=$PWD` when running pytest or committing, so tests import the worktree's `crr`, not the main checkout's.

## Review Focus

1. **A `config.toml` full of comments:** `crr config set` must leave the file byte-for-byte unchanged. Owned by Task 3, `test_set_never_touches_config_toml`.
2. **A corrupt or hand-edited `config_overrides.json`:** every crr command keeps working on `config.toml` plus the defaults, warns once on stderr, and `--effective --json` reports `"degraded": true`. Owned by Task 1, `test_corrupt_overrides_degrade_to_toml_and_defaults`, and Task 4, `test_effective_json_reports_a_degraded_overlay`.
3. **Stopping crr while sessions run in tmux:** `crr service stop` touches only the three units, and never `disable` or tmux. Owned by Task 6, `test_stop_only_stops_the_three_units`.
4. **Values typed by hand:**
   - `False` or `off` for a bool must parse.
   - `5.0` for an int must be refused cleanly.
   - A JSON list holding a number must be refused cleanly.

   Refused means exit 2 with a message, never a traceback. Owned by Task 3, `test_parse_config_value_*`.
5. **No systemd user manager** (WSL without systemd, or no user bus): `crr service status --json` reports `backend: "unsupported"` with a reason, never a traceback. Owned by Task 5, `test_status_reports_unsupported_without_a_user_manager`.

---

### Task 1: The config overlay store, and `Config` knowing about it

**Files:**
- Create: `crr/core/config_overrides.py`
- Modify: `crr/core/config.py` (the `Config` class, around lines 381-412), `crr/core/contracts.py`, `crr/cli.py` (`_load_config`, around line 84)
- Test: `tests/test_config_overrides.py` (new), `tests/test_config.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `config_overrides.FILENAME = "config_overrides.json"`
  - `config_overrides.OWNED_ELSEWHERE`: a frozenset of the five keys stored by the dashboard's stores (`remote_control_autokick`, `tunnel_provider`, `cloudflare_tunnel_name`, `cloudflare_hostname`, `discover_exclude_dirs`)
  - `class ConfigOverridesStore(state_dir: Path)`, with:
    - `read() -> dict[str, Any]`
    - `is_degraded() -> bool`
    - `set(key: str, value: Any) -> None`
    - `unset(key: str) -> bool`
  - `contracts.CONFIG_OVERRIDES_STORE_VERSION = 1`
  - `Config(overrides=None, *, overlay=None)`. Its `get` gives precedence to overlay, then overrides (config.toml), then default. `effective()` returns the origins `"override"`, `"configured"` and `"default"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_config_overrides.py`:
  - `test_overlay_beats_config_toml_beats_default`: `Config({"zombie_strikes": 4}, overlay={"zombie_strikes": 7}).get("zombie_strikes") == 7`, and `effective()["zombie_strikes"] == (7, "override")`.
  - `test_store_round_trips`: `set("zombie_strikes", 7)`, then `read() == {"zombie_strikes": 7}`. The file holds `{"v": 1, "values": {"zombie_strikes": 7}}`.
  - `test_set_refuses_unknown_or_mistyped_or_owned_keys`: `set("nope", 1)`, `set("zombie_strikes", "7")` and `set("tunnel_provider", "tailscale")` each raise `cfg.ConfigError`.
  - `test_read_drops_bad_values_one_at_a_time`: a file with `{"zombie_strikes": "x", "dashboard_port": 9000, "nope": 1}` reads as `{"dashboard_port": 9000}`.
  - `test_corrupt_overrides_degrade_to_toml_and_defaults`: a file holding `"{not json"` makes `read() == {}` and `is_degraded() is True`.
  - `test_unset_reports_whether_anything_was_removed`.
- [ ] **Step 2: Run them and see them fail.** Run: `PYTHONPATH=$PWD .venv/bin/pytest -q tests/test_config_overrides.py`. Expected: ImportError, because the module doesn't exist.
- [ ] **Step 3: Implement `crr/core/config_overrides.py` and the `overlay=` parameter on `Config`.**
  - Use `write_json_atomic` and `read_json_file` from `crr/core/journal.py`, as `ExclusionStore` does.
  - Type-check with the same rule `Config.__init__` uses: bool is never accepted for int, and the reverse.
- [ ] **Step 4: Add the version constant with its ledger comment, and a line in `tests/test_version_ledger.py`.**
- [ ] **Step 5: Write the failing CLI test** `test_load_config_layers_the_overlay`: write `config_overrides.json` in a tmp state dir, then check `cli._load_config().get(...)` returns the overlay value. Also add `test_load_config_warns_once_on_a_corrupt_overlay` (stderr names the file).
- [ ] **Step 6: Make `_load_config` pass `overlay=ConfigOverridesStore(sd).read()`.** It warns on stderr when `is_degraded()`.
- [ ] **Step 7: Run the touched test files and `lint-imports`, and confirm they pass.**
- [ ] **Step 8: Commit** with `feat: a crr-owned overlay for settings changes, layered over config.toml (#161)`.

### Task 2: An apply rule for every setting

**Files:**
- Modify: `crr/core/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `config.APPLY_KINDS = ("live", "restart-web", "restart-awake", "reinstall", "windows-installer")`
  - `config.APPLY_RULES: dict[str, str]`, covering exactly the keys of DEFAULTS

**Classification rule.** Give each key the strongest kind any of its readers needs, in this order: `windows-installer` > `reinstall` > `restart-web` > `restart-awake` > `live`.

- **windows-installer:** every key starting `boot_` or `harden_`.
- **reinstall:** `dashboard_port`, `watchdog_interval_seconds` and `web_restart_seconds`. These are baked into the units by `crr/adapters/systemd.py`.
- **restart-web:** any key read inside `_cmd_web` in `crr/cli.py`. It loads config once, at about line 5021, including inside the provider closures.
- **restart-awake:** any key read inside `_cmd_awake`, which loads config once at about line 1027.
- **live:** everything else. These are read fresh by each command, including every watchdog pass.

- [ ] **Step 1: Write the failing tests.**
  - `test_every_config_key_has_an_apply_rule`: `set(APPLY_RULES) == set(DEFAULTS)`, and every value is in APPLY_KINDS.
  - `test_known_apply_rules`:
    - `APPLY_RULES["dashboard_port"] == "reinstall"`
    - `APPLY_RULES["boot_preferred_tailnet"] == "windows-installer"`
    - `APPLY_RULES["zombie_strikes"] == "live"`
    - `APPLY_RULES["dashboard_poll_seconds"] == "restart-web"`
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Fill in `APPLY_RULES` by auditing each key's `config.get("…")` call sites** against the rule above. Note in a comment that a new DEFAULTS key fails the first test until it gets a rule.
- [ ] **Step 4: Run them and see them pass.**
- [ ] **Step 5: Commit** with `feat: every setting says how it takes effect (#161)`.

### Task 3: `crr config set` and `crr config unset`

**Files:**
- Modify: `crr/core/config.py`, `crr/cli.py` (the `config` parser near line 718, and `_cmd_config` near line 6609), `README.md` (the commands table), `CHANGELOG.md`
- Test: `tests/test_config.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes:
  - `ConfigOverridesStore` and `OWNED_ELSEWHERE` from Task 1
  - `APPLY_RULES` from Task 2
  - `SettingsStore.write_global_autokick(value: bool | None)`, `write_tunnel(provider, cloudflare_tunnel_name, cloudflare_hostname)` and `read_tunnel()` in `crr/core/settings.py`
  - `ExclusionStore.write(dirs)` in `crr/core/exclusions.py`
- Produces: `config.parse_config_value(key: str, text: str) -> Any`, which raises `ConfigError`.

**Parsing rules:**
- **bool:** case-insensitive `true`, `false`, `yes`, `no`, `on`, `off`, `1` or `0`.
- **int:** `int(text)`. `5.0` is refused.
- **float:** `float(text)`.
- **str:** the text as given.
- **list:** `json.loads(text)`, which must be a list of strings.

**Routing:**

| Key | Store |
|---|---|
| `remote_control_autokick` | `SettingsStore.write_global_autokick` (`unset` passes `None`) |
| `tunnel_provider`, `cloudflare_tunnel_name`, `cloudflare_hostname` | `SettingsStore.write_tunnel`, carrying the other two fields from `read_tunnel()`. `tunnel_provider` maps to the `provider` field. |
| `discover_exclude_dirs` | `ExclusionStore.write(list)` (`unset` writes `[]`) |
| everything else | `ConfigOverridesStore` |

Every write happens under `mutation_lock(state_dir)`.

**Output:** print `set KEY = VALUE (takes effect: <apply kind>)` on stdout. For `restart-web`, `restart-awake` and `reinstall`, add one stderr line naming the command to run:
- `systemctl --user restart crr-web`
- `systemctl --user restart crr-awake`
- `crr systemd --install`

- [ ] **Step 1: Write the failing tests.**
  - `test_parse_config_value_bools`: `"False"`, `"off"` and `"1"` parse.
  - `test_parse_config_value_refuses_a_float_for_an_int`: `"5.0"` raises.
  - `test_parse_config_value_refuses_a_list_with_a_number`: `'["a", 1]'` raises.
  - `test_set_writes_the_overlay`.
  - `test_set_never_touches_config_toml`: a commented `config.toml` is byte-identical after `set` and `unset`.
  - `test_set_routes_autokick_to_the_dashboard_store`.
  - `test_set_routes_one_tunnel_field_and_keeps_the_others`.
  - `test_set_routes_exclusions_to_their_store`.
  - `test_unset_removes_the_override`.
  - `test_set_unknown_key_exits_2`.
  - `test_set_bad_value_exits_2_with_a_message`: no traceback appears on stderr.
  - `test_set_names_the_restart_for_a_web_key`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement `parse_config_value` and the `set` and `unset` subcommands.** Keep `crr config --effective` working without a subcommand.
- [ ] **Step 4: Run the touched files and see them pass.**
- [ ] **Step 5: Add a README row and a CHANGELOG entry, then commit** with `feat: crr config set/unset, never writing config.toml (#161)`.

### Task 4: `crr config --effective --json`, seeing every layer

**Files:**
- Modify: `crr/core/config.py`, `crr/core/contracts.py`, `crr/cli.py` (`_cmd_config`), `tests/test_version_ledger.py`, `AGENTS.md`
- Test: `tests/test_config.py`, `tests/test_contracts.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Tasks 1 to 3. Also `SettingsStore.read_global_autokick()`, `read_tunnel()` and `is_degraded()`, `ExclusionStore.read()`, and `exclusions.effective(configured, managed)`.
- Produces:
  - `config.effective_payload(config: Config, *, owned: Mapping[str, Any], degraded: bool) -> dict`. `owned` holds the five keys' store values, already resolved by the CLI: present means overridden.
  - `contracts.CONFIG_CONTRACT_VERSION = 1`
  - `contracts.CONFIG_PAYLOAD_KEYS = ("contract", "defaults_version", "keys", "degraded")`
  - `contracts.CONFIG_KEY_FIELDS = ("value", "default", "origin", "type", "apply")`
  - `contracts.validate_config_payload(payload)`

**Origins:** one of `default`, `configured` or `override`.
- `discover_exclude_dirs` shows `exclusions.effective(configured, managed)` as its value. Its origin is `override` whenever the managed list is non-empty.
- `type` is one of `bool`, `int`, `float`, `str` or `list`.
- `degraded` is true when either the overlay (Task 1) or the dashboard's `settings.json` (`SettingsStore.is_degraded()`) is unreadable. Either way, some origins may be wrong.

- [ ] **Step 1: Write the failing tests.**
  - `test_effective_payload_validates`.
  - `test_dashboard_overrides_show_as_override`: a stored tunnel provider and autokick both report origin `override` with the stored value.
  - `test_effective_json_reports_a_degraded_overlay`.
  - `test_effective_text_shows_override_origins`: the existing text output now prints `(override)` too.
  - `test_config_contract_ledger_has_no_holes`, in the ledger test file.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement `effective_payload`, the contract and validator, and `--json`.** The CLI resolves `owned` from the two stores and validates before printing.
- [ ] **Step 4: Run them and see them pass.** Update AGENTS.md's Contracts line.
- [ ] **Step 5: Commit** with `feat: crr config --effective --json reports every layer (#161)`.

### Task 5: `crr service status`

**Files:**
- Create: `crr/core/service.py`
- Modify: `crr/adapters/systemd.py`, `crr/core/contracts.py`, `crr/cli.py` (a new `service` subparser), `tests/test_version_ledger.py`, `AGENTS.md`
- Test: `tests/test_service.py` (new), `tests/test_systemd.py`, `tests/test_cli.py`

**Interfaces:**
- Produces:
  - `service.UnitState`, a NamedTuple: `name: str`, `unit: str`, `installed: bool`, `enabled: bool`, `active: bool`
  - `service.overall_state(units: Sequence[UnitState]) -> str`, returning one of:
    - `"not-installed"`: nothing installed
    - `"running"`: every installed unit active
    - `"stopped"`: every installed unit inactive
    - `"partial"`: anything else
  - `service.status_payload(backend: str, reason: str, units: Sequence[UnitState], dashboard_url: str) -> dict`
  - `contracts.SERVICE_CONTRACT_VERSION = 1`
  - `contracts.SERVICE_PAYLOAD_KEYS = ("contract", "backend", "reason", "state", "services", "dashboard_url")`
  - `contracts.SERVICE_UNIT_FIELDS = ("name", "unit", "installed", "enabled", "active")`
  - `contracts.validate_service_payload`
  - `systemd.CRR_UNITS`: a tuple of `(name, unit)` pairs: `(("watchdog", TIMER_NAME), ("dashboard", WEB_SERVICE_NAME), ("keep-awake", AWAKE_SERVICE_NAME))`
  - `systemd.unit_states(timeout: float) -> list[service.UnitState] | None`. It parses `systemctl --user show <units> -p Id,LoadState,UnitFileState,ActiveState`, and returns None when the user manager can't be reached.

**Backend and URL:**
- `backend` is `"systemd"` on Linux when `unit_states` answers. Otherwise it's `"unsupported"`, with a reason such as "no systemd user manager" or "not Linux". A Scheduled Tasks install shows as `not-installed`, with a reason pointing at `crr systemd --install`.
- `dashboard_url` is `http://127.0.0.1:<dashboard_port>/`.

- [ ] **Step 1: Write the failing tests.**
  - `test_overall_state` covers the four states, in a table.
  - `test_unit_states_parses_systemctl_show`: fake `subprocess.run` output for three units, including one with `LoadState=not-found`.
  - `test_status_reports_unsupported_without_a_user_manager`.
  - `test_service_status_json_validates`.
  - The ledger test.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement the core module, the adapter, and `crr service status [--json]`.** The text form prints one line per unit, plus the overall state.
- [ ] **Step 4: Run them and see them pass.** Update AGENTS.md.
- [ ] **Step 5: Commit** with `feat: crr service status reports crr's own units (#161)`.

### Task 6: `crr service start` and `crr service stop`

**Files:**
- Modify: `crr/adapters/systemd.py`, `crr/cli.py`, `README.md`, `CHANGELOG.md`
- Test: `tests/test_systemd.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `systemd.CRR_UNITS` and `systemd.unit_states` from Task 5.
- Produces:
  - `systemd.start_argv() -> list[str]`, which is `["systemctl", "--user", "start", <the three units>]`
  - `systemd.stop_argv() -> list[str]`, which is `["systemctl", "--user", "stop", <the three units>]`

**Behaviour:**
- Both refuse with exit 1 when the state is `not-installed`, and with exit 2 when the backend is unsupported.
- After a stop, print on stdout: `stopped; crr comes back at the next WSL restart or reboot, or with crr service start`.

- [ ] **Step 1: Write the failing tests.**
  - `test_stop_only_stops_the_three_units`: the argv is exact, nothing anywhere contains `disable`, and nothing names `tmux`.
  - `test_start_argv`.
  - `test_stop_refuses_when_not_installed`.
  - `test_stop_says_when_crr_comes_back`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement both.** Run the argv with the existing `subprocess` pattern, and pass systemctl's stderr through on failure.
- [ ] **Step 4: Run them and see them pass.**
- [ ] **Step 5: Add a README row and a CHANGELOG entry, then commit** with `feat: crr service start/stop, a runtime stop that never disables (#161)`.

### Task 7: `crr logs`

**Files:**
- Modify: `crr/adapters/systemd.py`, `crr/core/config.py` (a new key), `crr/cli.py`, `README.md`, `CHANGELOG.md`
- Test: `tests/test_systemd.py`, `tests/test_cli.py`, `tests/test_config.py`

**Interfaces:**
- Produces:
  - `systemd.LOG_UNITS = {"revive": SERVICE_NAME, "web": WEB_SERVICE_NAME, "awake": AWAKE_SERVICE_NAME}`. The timer has no log output; its service does.
  - `systemd.journal_argv(unit: str, lines: int, follow: bool) -> list[str]`, which is `["journalctl", "--user", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"]`, plus `"-f"` when following.
  - A new DEFAULTS key, `logs_default_lines = 200`. It bumps `CONFIG_DEFAULTS_VERSION` to 30 with a ledger entry, and its apply rule is `live`.

**CLI:** `crr logs [--service revive|web|awake] [--lines N] [--follow]`. The default service is `revive`.
- Without `--follow`, run journalctl and pass its output through.
- With `--follow`, hand over through the existing `cli._exec` seam, so killing the process stops journalctl.
- An unsupported backend exits 2.

- [ ] **Step 1: Write the failing tests.**
  - `test_journal_argv`, with and without `-f`.
  - `test_logs_follow_execs_journalctl`: stub `cli._exec`, and assert its argv.
  - `test_logs_default_lines_come_from_config`.
  - `test_logs_unsupported_exits_2`.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run them and see them pass,** including the config ledger test.
- [ ] **Step 5: Add a README row and a CHANGELOG entry, then commit** with `feat: crr logs for crr's own services (#161)`.

### Task 8: `crr qr --json`

**Files:**
- Modify: `crr/cli.py` (`_cmd_qr`, near line 1739), `crr/core/contracts.py`, `tests/test_version_ledger.py`, `AGENTS.md`, `README.md`
- Test: `tests/test_cli.py`, `tests/test_contracts.py`

**Interfaces:**
- Consumes: `_tunnel_selection`, `_tunnel_provider`, and the provider's `advertise_url()` and `setup_hint()`, already used by `_cmd_qr`.
- Produces:
  - `contracts.QR_CONTRACT_VERSION = 1`
  - `contracts.QR_PAYLOAD_KEYS = ("contract", "provider", "url", "loopback_url", "hint", "dashboard_reachable")`
  - `contracts.validate_qr_payload`

**Fields:**
- `url` is the advertised URL, or null.
- `provider` is the provider name, or null.
- `hint` is the setup hint, or null.
- `dashboard_reachable` comes from a stdlib `urllib` GET of `<loopback_url>api/version`, within `interop_timeout_seconds`.

- [ ] **Step 1: Write the failing tests.**
  - `test_qr_json_with_an_advertised_url`.
  - `test_qr_json_without_a_tunnel`: url is null, and hint is present.
  - `test_qr_json_reports_an_unreachable_dashboard`.
  - The validator and ledger tests.
- [ ] **Step 2: Run them and see them fail.**
- [ ] **Step 3: Implement `--json`.** The existing terminal output doesn't change.
- [ ] **Step 4: Run them and see them pass.** Update AGENTS.md and README.
- [ ] **Step 5: Commit** with `feat: crr qr --json for the tray app (#161)`.

### Task 9: Whole-branch check

- [ ] **Step 1:** Run the full suite and `lint-imports` from the worktree, and confirm both are green.
- [ ] **Step 2:** On a real systemd host, run each new command by hand.
  - **Read-only commands:**
    - `crr config --effective --json | python3 -m json.tool`
    - `crr service status --json`
    - `crr logs --lines 5`
    - `crr qr --json`
  - **Commands that change state:** run `crr service stop` and `crr service start` only with the owner's go-ahead, since they stop and start the live services.
- [ ] **Step 3:** Open the PR with `Refs #161` (the issue stays open for parts 2 to 4), wait for green CI, and merge.
