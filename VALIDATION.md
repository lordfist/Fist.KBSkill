# Verification

## 0.4.0 v08 engine - 2026-10-30

Run on Linux only (Python 3.13.16, locked dependencies; the lockfile was updated with uv 0.13.5 and changed only the package version).

- 75 source tests ran: 74 passed and 1 was skipped, the PowerShell hook-script test, because PowerShell 7 was not available on this host. New or rewritten for 0.4.0:
  - `test_engine` (22 tests, run through the public CLI): operations and refusals, history and restore, paging of large and imported records, revision safety, fault outcomes, request IDs, tool identity, orientation and search, migration, preparation, source revisions with and without Git, ranking, guest reads, and a check that USAGE.md matches the schema and names every error code;
  - migration of a 0.3.x KB in `test_setup`, `test_project_mcp` and `test_session_context`;
  - the CLI, MCP, runtime and hook suites for the two-tool interface.
- The engine files are the v08 release tools named in PROVENANCE.json, with the port changes listed there. The release's own engine test cases were ported, except one that needs data outside this repository.
- Mutation controls: each of ten deliberate faults made its targeted test fail:
  - removing `-P` from the API child or from the CLI's engine child;
  - accepting changed package bytes in the tool check or in the guest reader;
  - migrating a null purpose as "None";
  - ignoring the hook's size limit;
  - dropping the 0.3.x message;
  - writing without the lock;
  - keeping a failed migration's folder;
  - passing requests on the command line instead of stdin.
- Every JSON example in USAGE.md and EVIDENCE.md ran in order on disposable KBs, together with the commands around them: preparation, deep validation, a source revision without Git, guest reads and turning guest reads off.
- Migration from 0.3.x: a KB created and written by the 0.3.0 package migrated through `kb_setup` mode `migrate`. Every field was rebuilt and compared, none spilled, and the old KB's bytes were unchanged. This check found that a null 0.3.x purpose migrated as the text "None"; it is fixed in the port.
- Plugin checks with Claude Code 2.1.314:
  - `claude plugin validate` passed with the two earlier warnings (no marketplace description, no icon);
  - an isolated install from the local checkout lists version 0.4.0 with 2 skills, 1 SessionStart hook and 1 MCP server;
  - always-on cost is about 155 tokens (0.3.0: about 148, same check);
  - on invoke, the working skill costs about 3.5k tokens (0.3.0: 2.8k) and the setup skill about 1.7k (0.3.0: 1.2k). The skill now carries the kinds and the write operations, which the generic MCP schema cannot describe.
- The MCP server lists 4 tools in 3,278 bytes of tool definitions; 0.3.0 listed 10 tools in 7,698 bytes.
- The demo passed and the release outputs were rebuilt on Linux. The wheel, installed into a fresh environment, ran the demo, a write, `admin verify-tools` and `guest info`.

**Qualification limit:** not run on Windows or inside Claude Code or Codex chats: the PowerShell launcher and hook script, the folder picker, the Codex trust review, re-injection after a real compaction, and agent behaviour with the two tools. The field set and size limits come from the v08 release, which calibrated them on one KB.

## 0.3.0 search ordering - 2026-10-25

Run on Linux only (Python 3.13.16, locked dependencies; lockfile regenerated with uv 0.13.5).

- All 59 source tests passed, including the new `test_matching` (9 tests) and the PowerShell session-start check under PowerShell 7.4.6.
- `test_matching` on 0.2.3 source: the 5 ordering tests fail as expected; its controls pass, including the pinned 0.2.3 search and brief match sets for 26 and 7 queries.
- On a copy of a real 54-record KB, 30 queries through the public API returned the same search matches and brief match counts under 0.2.3 and 0.3.0; 20 came back in a different order.
- The engine change is identical to the source candidate it was ported from, which passed that package's own 96-test suite, mutation controls for each rule, and an unchanged-match comparison over 96 queries on three corpora.
- The demo passed and the release outputs were rebuilt on Linux.

**Qualification limit:** not run on Windows or inside Claude Code or Codex. The route bonus for partly named routes (10) and the match strengths (1, 2/3, 1/3) are reasoned choices, not tuned on user data.

## GitHub installation on Windows - 2026-10-24

Tested the published 0.2.3 package at commit `4f55cac66aebda4bcbdee542cdb00ee52fed0edc`, using Codex CLI `0.164.0-alpha.11.1` and Claude Code `2.1.304` in disposable, separate client configuration directories.

- Both clients added `lordfist/Fist.KBSkill` directly from GitHub, installed `fist-kb@fist-kb-local`, and listed version 0.2.3 as enabled. Claude's shell equivalents of the documented in-session commands were exercised.
- Each installed copy started through its own declared Windows MCP launcher. All ten tools were available; package integrity passed; explicit-path setup created a disposable KB; `kb_core` returned the complete core matching its saved file.
- A separate Codex app server discovered all ten tools from the GitHub-installed plugin with no tool error, without starting a chat or making a model call.
- Claude manifest validation passed with an advisory about the missing marketplace description. The check also identified the missing README install line corrected in this documentation update.
- No existing client profile or agent KB was changed. A temporary Git process briefly held a cleanup lock; after it exited, the exact test folder was removed and its absence verified.

**Scope:** this verifies GitHub acquisition, installation, tool startup and explicit-path KB setup on Windows. It does not test Claude's interactive slash-command panel, the folder picker, hook trust or post-compaction injection in an actual chat. The earlier hook qualification limits below still apply.

## 0.2.3 context and isolation changes - 2026-10-24

Run on Linux only (Python 3.13.16, locked dependencies; lockfile regenerated with uv 0.13.5).

- All 50 source tests passed: the previous 42, one working-directory isolation test and seven session-context tests. One of these runs `launch.ps1` (with a stand-in uv) and `session-context.ps1` under PowerShell 7.4.6 and checks that the hook finds the launcher's runtime and prints the complete core; it is skipped where `pwsh` is unavailable.
- Mutation controls failed as intended: removing `-P` from each of the three child launches (CLI, API, `kb_client.call_tool`), changing the hook's environment key, dropping the 6,000-byte check, or printing output without a prepared runtime.
- The Codex Windows hook command, with `pwsh` in place of `powershell.exe`, printed the complete core when run through `sh` and through PowerShell 7 as the outer shell.
- `claude plugin validate` passed with the two earlier warnings (no marketplace description, no icon). An isolated Claude Code 2.1.309 install lists 2 skills, 1 SessionStart hook and 1 MCP server; the working skill is about 2.8k tokens when invoked.
- The demo passed and the release outputs were rebuilt on Linux.
- Basis for the consultation change: on a 47-record copy of a real KB, the full core was about 850 estimated tokens and each default brief about 1,900. 0.2.2 asked for a brief before every request.

**Qualification limit:** not run on Windows: the hooks inside Claude Code or Codex, the Codex trust review, cmd.exe or Windows PowerShell 5.1 handling of the commands, re-injection after a real compaction, and hook timing on first launch. Agent behaviour under the new consultation rule was not measured.

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

The current chat did not gain the newly installed plugin tools without a new chat/reload. A complete fresh-chat onboarding check remains for the actual Codex app. Claude Code host installation was not tested for the 0.2.0 baseline; the later GitHub installation check above covers 0.2.3 installation and tool startup, not interactive agent behaviour. Linux/macOS plugin launchers, optional PDF preparation and other Python versions were not qualified.

## Layout and history

The 0.2.0 source contains one implementation and shared skills. Distribution outputs are one wheel, one combined plugin ZIP and a manifest. Old expanded plugin copies and 0.1.0 release outputs were removed. The owner also requested removal of the private predecessor archive; only compact historical identities/results are retained.
