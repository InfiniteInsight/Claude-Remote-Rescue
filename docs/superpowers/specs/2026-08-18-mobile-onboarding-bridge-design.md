# Mobile onboarding bridge — QR first-contact, PWA permanence, tailnet launcher

**Status:** design · 2026-08-18
**Follow-on to:** the Tailscale-dashboard access model (README "Reaching the dashboard on your tailnet")
**Scope:** getting the tailnet dashboard onto — and back onto — a phone with the least friction, plus a cross-machine list. Three independently-shippable phases. No change to the session-management core.

---

## Why

crr installs on the *computer*, but the dashboard is used from a *phone*. Today the only bridge between them is the user reading a URL off the computer and typing it into the phone — and re-finding it every time after. That is the whole friction. It splits into two distinct problems plus a convenience:

- **(a) First contact** — get the dashboard URL from the computer (where it's known) onto the phone the *first* time.
- **(b) Repeat access** — never have to re-type or re-find it afterward.
- **(c) Cross-machine** — crr is per-host (one dashboard per machine); reaching a *second* machine's dashboard repeats the whole problem.

Two facts constrain the solution (verified against Tailscale docs, 2026-08-18):
- **Tailscale won't surface a `serve` URL as a tappable link in its mobile app** — there is no hook for crr to inject one (open FR tailscale/tailscale#8267). The bridge must come from crr.
- **QR is Tailscale's own idiom** for "get this onto a phone" (device-add uses it). It is the natural first-contact tool.

This design does **not** try to make one machine aggregate others (no central server, no cross-host calls, no secrets) — that would betray crr's stdlib-only, zero-network-surface ethos. The browser, already on the tailnet, does all cross-machine navigation itself.

## The three phases (ship in this order — most-friction-first)

| Phase | Solves | Deliverable |
| --- | --- | --- |
| **1 — QR bridge** | (a) first contact | `crr qr` prints a scannable QR of this machine's tailnet URL in the terminal (and at end of setup); a `/qr.svg` route + a small dashboard affordance bridges further devices |
| **2 — PWA** | (b) repeat access | dashboard is installable ("Add to Home Screen") → a permanent home-screen icon |
| **3 — Launcher** | (c) cross-machine | a lazy "Machines" dashboard panel listing every `tag:crr` node's dashboard as a link, with an on-tailnet badge |

Each phase is a separate implementation plan producing working, tested software on its own. Phases 1 and 3 share one new `tailscale` adapter.

---

## Architecture (crr's one-way layering: `cli → adapters → core`)

### Shared: `crr/adapters/tailscale.py`

A thin, cross-platform wrapper over the `tailscale` CLI, generalizing the existing precedent in `cli.py:4694` (`_current_tailnet_account`, which already parses `tailscale.exe status --json` for `CurrentTailnet.Name`). Resolve the binary via `shutil.which("tailscale")` / `"tailscale.exe"`.

- `status() -> dict | None` — run `tailscale status --json` (own timeout, `interop_timeout_seconds`), parse JSON. **Tri-state:** returns `None` on missing binary / timeout / nonzero exit / unparseable output (never raises). Pure command builder `_status_cmd()` asserted as argv in tests.
- `serve_status() -> dict | None` — run `tailscale serve status --json`; same tri-state. Used by Phase 1 to confirm serve is actually live on 443 before encoding a URL (see error handling).

The adapter does **no** interpretation of the parsed structure beyond returning it — all selection/URL-building is pure core (below), so it is exhaustively testable from captured JSON fixtures.

**URL construction rule (applies everywhere a dashboard URL is built):** use the node's **`DNSName`** (strip the trailing dot), **never `HostName`**. Rationale, from real data on this tailnet: this WSL host reports `Self.DNSName = hedylamarr-1.tail3af2d9.ts.net` while a *separate* peer's `HostName` is `HedyLamarr`. `HostName` is the human label and can collide / duplicate; `DNSName` carries Tailscale's disambiguation suffix (`-1`) and is what the `serve` HTTPS cert is issued for. Building from `HostName` would produce a URL whose cert fails and/or points at the wrong node.

### Phase 1 — QR bridge

- **`crr/core/qr.py`** — a vendored, dependency-free, pure-Python QR encoder plus two pure renderers:
  - `encode(text) -> Matrix` (a 2-D grid of booleans). QR byte-mode, error-correction level chosen to comfortably fit a tailnet URL (~40–60 chars → a low version; level M).
  - `to_terminal(matrix) -> str` — Unicode half-block rendering (`█ ▀ ▄ ' '`), with a quiet-zone border, sized to scan reliably off a screen.
  - `to_svg(matrix) -> str` — a minimal self-contained SVG (black/white rects) for the web route.
  All three are pure functions — no I/O, no globals — so they unit-test as data transforms. This is the one chunk of real vendored code the design accepts; justified because true first-contact must work over SSH/headless where no browser exists to run a JS encoder.
- **cli `crr qr`** — resolves this machine's dashboard URL from the `tailscale` adapter, prints `to_terminal(encode(url))` plus the URL in text beneath. Also invoked at the end of `bootstrap.sh` / setup so the very first thing after install is a scannable code.
- **web `/qr.svg`** — serves `to_svg(encode(self_url))`; a small "📱 Add a device" affordance on the dashboard reveals it, so a phone already on the dashboard bridges the next device. Reuses the *same* core encoder — no second (JS) implementation.

### Phase 2 — PWA

- **web routes:** `/manifest.webmanifest` (name/short_name "CRR", `display: standalone`, `start_url: /`, theme/background color, icon references), an **app icon** bundled as a static asset (a simple crr glyph; provide `apple-touch-icon` PNG for iOS and 192/512 PNGs for Android install), and a **service worker** at `/sw.js`.
- **`page.html`:** add `<link rel="manifest">`, `apple-touch-icon`, and the iOS `apple-mobile-web-app-*` meta tags.
- HTTPS (a hard PWA requirement) is already provided by `tailscale serve`.

> **CRITICAL — the service worker must not defeat the page self-heal.** `web.py` deliberately sets `Cache-Control: no-store` on everything so the PAGE_VERSION vs `/api/version` self-update can never be served a stale page. A conventional cache-first "app shell" SW would cache `page.html` and reintroduce exactly that staleness bug. Therefore the SW in this design:
> - **must never cache HTML responses or any `/api/*` response** — those are always network-only pass-through;
> - may cache **only** immutable static assets (icons, manifest) if desired;
> - exists primarily to satisfy installability, not to provide offline use.
>
> Also evaluate whether current iOS/Chrome installability is met by **manifest + icons alone**; if a SW with a `fetch` handler is still required for the install prompt, ship the network-pass-through SW above (a `fetch` handler that just `fetch(event.request)` for navigations/API). Scope is *installability only* — no offline-caching ambitions.

### Phase 3 — Launcher

- **`crr/core/launcher.py`** — pure:
  `plan_launcher(status: dict | None, *, tag: str, self_dnsname: str | None) -> list[MachineRow]`
  where `MachineRow` carries `{name, url, online, is_self}`. Behavior: return `[]` if `status is None`; otherwise select `Self` + every `Peer` whose `Tags` contains `tag`; build `url = "https://" + dnsname.rstrip(".") + "/"` from **`DNSName`**; `name` from `HostName` (display only, never the URL); `online` from the node's `Online` bool; `is_self` by matching `self_dnsname`. Sort self-first, then online-first, then by name. Fully branch-tested.
- **web `/api/machines`** — lazy (loaded when the panel is expanded, like `/api/discoverable`), returns the rows through a versioned contract (new `MACHINE_ROW_KEYS` + validator in `contracts.py`, its own version constant).
- **`page.html`:** a collapsible **"Machines"** panel rendering rows — `name` and the on-tailnet badge via `textContent`, the link via an `href` attribute (untrusted-field safety, matching the existing panels). The badge text is **"on tailnet"** / **"offline"**, never "live" (see below).
- **config:** `launcher_tag` (default `"tag:crr"`), so the tag is not hard-coded. `CONFIG_DEFAULTS_VERSION` bump.

---

## Data flow, error handling, degradation

- All Tailscale reads are **local** (`tailscale status --json`, `tailscale serve status --json`). No cross-host calls, no central server, no API token, no secrets. Cross-machine navigation is done by the user's browser (no `fetch`, so crr's no-CORS CSRF posture is untouched).
- **Degrade, never crash** (the established tri-state adapter pattern):
  - `crr qr`: if the adapter can't resolve a live serve URL (tailscale missing / logged out / **serve not configured on 443**), it prints the loopback URL and a one-line hint (`run: tailscale serve --bg 8377`) instead of encoding a URL that dead-ends. It confirms serve via `serve_status()`, not merely `status()` — a node can be on the tailnet with nothing listening on 443.
  - Machines panel: `status is None` or no tagged nodes → the panel renders empty with a short "no tag:crr machines found — see setup" note. Never an error.
- **The online badge means "reachable on the tailnet," not "the dashboard process is up."** crr cannot cheaply probe a peer's dashboard (that would be the CORS-blocked cross-origin fetch), so the badge is honest about what it reflects. A machine can read "on tailnet" while its `crr web` service is down.

## Security / safety

- No new inbound surface: `/qr.svg`, `/manifest.webmanifest`, `/sw.js`, `/api/machines` are all GET, same-origin, behind the existing Host allowlist; no new POST/actions. No remote control of other machines — the launcher only emits links.
- New *local* subprocess dependency (`tailscale`) in the serving path; fully guarded by the tri-state adapter so absence degrades to empty, never a 500.
- The dashboard already renders on the tailnet (the auth boundary); listing your own tagged machines exposes nothing you don't already own. Peer-supplied fields (`HostName`) render via `textContent`; URLs are built from `DNSName` (metacharacter-free Tailscale names) into `href` only.
- QR encodes only the machine's own dashboard URL — no secrets, no tokens.

## Testing

- **QR encoder (`crr/core/qr.py`):** QR output is **not** a canonical function of the input (mask selection picks the lowest-penalty of 8 candidates; version/ECC affect the matrix), so there is no published input→matrix vector to assert against without byte-matching a specific reference implementation. Verify by **round-trip decode**: encode a set of strings (incl. a real tailnet URL), decode with a QR-decoder used as a **test-only dependency** (dev/test deps are allowed — *runtime* deps stay zero), assert the decode equals the input. Renderers (`to_terminal`, `to_svg`) tested as pure matrix→string (quiet-zone present, dimensions correct, deterministic). One **manual phone-scan** is the acceptance check.
- **`tailscale` adapter:** command builders asserted as exact argv; `status()`/`serve_status()` parse fed **captured JSON fixtures**, including a **tagged-node fixture** (hand-authored, since the live tailnet currently has no tagged node — see verification note), plus the degrade cases (missing binary, nonzero, garbage output → `None`).
- **`plan_launcher`:** pure, every branch — `None` status → `[]`; tag match vs no-match; self selection + `is_self`; URL built from `DNSName` not `HostName` (a fixture where they differ, e.g. `hedylamarr-1` vs `HedyLamarr`, guards this regression); online/offline; sort order.
- **PWA:** `page.html` scripts still pass `node --check` (existing CI gate); a test asserts the SW source contains **no HTML/`/api` caching** (guard against the self-heal regression); manifest route returns valid JSON with required keys.
- **No test** runs real tailscale, installs a real PWA, opens a real browser, or hits the network.

## Open verification (record, resolve at first tagged deploy)

The peer-`Tags` field is confirmed to exist and serialize (`ipnstate.PeerStatus.Tags *views.Slice[string]`, `json:",omitempty"`; `Self` is a `*PeerStatus`) — so the local no-API approach is sound; the field is simply omitted until a node is tagged. **Belt-and-suspenders live check** before/at Phase 3 rollout: on **lovelace** (a clean single node — not this WSL host, which is a duplicate `hedylamarr-1` registration), add `tagOwners` for `tag:crr` in the tailnet ACL, `tailscale set --advertise-tags=tag:crr`, then confirm the node's peer object carries `Tags` in `tailscale status --json` read from another node. Fold the exact steps into the README setup docs (they double as the user's own one-time tagging instructions).

## Docs (part of the phases, not an afterthought)

- README: a "Get the dashboard on your phone" subsection — `crr qr`, the PWA install gesture, and the `tag:crr` setup (ACL `tagOwners` + `--advertise-tags`) for the launcher.
- `crr qr` added to the Commands table.

## Global constraints (bind every phase)

- **Zero runtime dependencies.** The QR encoder is *vendored* (dependency-free); a QR *decoder* appears only as a test/dev dependency. The web server stays stdlib-`http` only.
- **One-way layering** (`.importlinter`-enforced): `cli → adapters → core`; `crr.core` imports neither adapters nor cli. QR encoder and `plan_launcher` are pure core; the `tailscale` subprocess and the `os`/tty/exec/serve reads live in adapters/cli.
- **`textContent` for untrusted fields**, `href` for links; no new CORS headers; no new POST routes.
- **PAGE_VERSION** bumped on every `page.html` change (enforced by `test_page_version_guard.py`); contract/config version constants bumped with their payloads.
- **Test-first**, and no test touches real tailscale / real PWA install / the network.

## Out of scope

- Aggregating other machines' *session data* into one view (would require cross-host calls — deliberately excluded).
- A public/Funnel dashboard, a web terminal, or any custom-domain HTTPS termination.
- Probing whether a peer's dashboard process is actually up (CORS-blocked; the badge reflects tailnet reachability only).
- Auto-tagging machines during setup (tagging changes node ownership/key-expiry semantics — left as a documented, deliberate one-time user action).
