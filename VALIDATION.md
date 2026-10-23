# Verification

## 0.2.2 review corrections - 2026-10-24

- All 42 source tests passed across six suites: 11 setup, 4 review-specific API, 3 fixed MCP, 1 project MCP, and 23 CLI/process checks.
- Full core matches canonical data, stays read-only, reports its actual roots, and rejects insufficient budgets or stale revisions.
- Fixed-A/project-B tests confirm status cannot describe B as the active fixed connection and setup cannot mutate B.
- An off-project source was rejected without canonical changes; an in-bound source was registered, prepared and verified in the separate store.
- A controlled lock-cleanup failure after a real disposable commit preserved completed/verified write status, a single revision advance, the remaining lock and attributable error diagnostics. No retry occurred.
- Runtime verification, two-project isolation, restart, guest read-only behavior and prior transaction checks passed. All owned fixtures were removed. Initial sandbox MCP attempts stalled; explicitly permitted reruns completed.
- Six public workflow documents, 21 local links, JSON examples and skill metadata were checked. The optional project-entry loading chain was reviewed against the implementation.

**Qualification limit:** persistent entry installation is an opt-in action performed by the agent using host file tools. No real project entry was installed during these tests. Fresh-session skill loading, automatic memory use and compaction recovery in actual Codex and Claude Code conversations are not yet qualified. Service/tool success is not a claim of Seed-equivalent agent behavior.

## 0.2.1 documentation checks - 2026-10-24

- The four JSON requests in USAGE.md passed in a disposable KB: new record, correction, new route and route update.
- Each request was drafted, reviewed across forced preview pages, committed and read back. Corrected conditions and uncertainty remained discoverable through the topic after reopening.
- The documented full-core CLI output matched the complete core.json. Skill metadata and relative documentation links were checked. Test data was removed.
- This release changes documentation and package version metadata. The KB engine and launcher are unchanged; the prior runtime qualification below was not rerun as a new full suite.
- These checks do not establish automatic skill loading, context retention or equivalent behaviour from every agent.

## 0.2.0 runtime baseline

Fist KB 0.2.0, checked on 2026-10-23.

Windows backend/package checks passed:

- 34 source tests: CLI, transactions, guest reads, process handling, guided setup, cancellation, existing-data preservation, selection recovery and project isolation.
- Real MCP startup without a KB, create/select, reviewed write, restart and simultaneous reads from two different project KBs.
- The combined ZIP was extracted into a disposable location and launched using its actual Windows MCP manifest command.
- First launch with no uv on PATH: private uv installation, managed Python download, dependencies, MCP workflow and restart.
- A fresh wheel installation passed the same project/restart workflow.
- The actual Windows folder picker opened and returned the user's selection. That interactive check did not create or change files in the selected folder.
- The installed Codex CLI accepted the local marketplace and installed Fist KB 0.2.0; readback reported installed and enabled.
- A separate instance of the actual Codex app-server (0.163.0-alpha.11.2) negotiated the installed plugin and discovered all nine tools without creating a chat or selecting a KB.
- That client check exposed a uv minor-version junction lookup failure. Pinning the launcher to the exact tested Python 3.12.15 resolved startup; subsequent actual-client discovery returned no tool error.

Tests use synthetic data and owned temporary directories, with cleanup verified. They do not adopt or migrate live agent KBs.

The full source suite ran on Python 3.12.14. The first-launch test downloaded and used managed CPython 3.12.15. Both used Windows, uv 0.13.5 and the locked MCP SDK 1.30.0. No interpreter is bundled.

## Limits

The current chat did not gain the newly installed plugin tools without a new chat/reload. A complete fresh-chat onboarding check remains for the actual Codex app. Claude Code host installation was not tested; the launcher and engine are shared, but host behaviour is a separate check. Linux/macOS plugin launchers, optional PDF preparation and other Python versions were not qualified.

## Layout and history

The 0.2.0 source contains one implementation and shared skills. Distribution outputs are one wheel, one combined plugin ZIP and a manifest. Old expanded plugin copies and 0.1.0 release outputs were removed. The owner also requested removal of the private predecessor archive; only compact historical identities/results are retained.
