# Using Fist KB

Examples use the installed `fist-kb` CLI. From a source checkout, prefix commands with `uv run --locked`. Plugins expose the same engine through MCP as `kb_read` and `kb_write`; see [client integration](INTEGRATIONS.md). Paths, IDs, names and revisions below are examples: use the actual selected KB and inspected values.

## Everyday memory workflow

For an agent using Fist KB as its working memory:

1. Start from the orientation (`kb_read` with no keys): the plugin's session-start hook adds it at session start and after compaction; otherwise read it once. It holds the complete core, the current task, recent writes, an index and pinned records.
2. Read with `kb_read` at the start of a task, when the work moves to a different topic, and before answers or decisions that depend on project knowledge. Acknowledgements and follow-ups the retrieved material already answers need no new call.
3. Read identified records when their detail matters. Search when routes and orientation do not answer the question, when evidence conflicts, and before concluding knowledge is missing.
4. Within authorized maintenance, save material decisions, corrections, findings and outcomes with `kb_write` as they occur. Check existing records first; avoid duplicate notes and routine chatter.
5. Read the echo of each write. After compaction or a new session, make sure the [operating skill](../skills/fist-kb/SKILL.md) and the orientation are present, then retrieve relevant records before resuming.

This routine requires the host to supply the skill and the agent to follow it. The session-start hook adds the orientation at session start, resume, `/clear` and compaction, not on every turn; nothing captures conversations. Explicit read-only instructions still prohibit writes.

## Choose the data directory

The plugin's `kb_status(project=..., verify_runtime=true)` checks packaged-runtime integrity during onboarding/recovery and returns the selected `kb_root` and `evidence_root`. Use guided setup if none is selected. A service-ready result does not prove the operating skill is loaded; complete the [working-memory setup](INTEGRATIONS.md#enable-project-memory). Direct CLI initialization uses an existing project directory:

```text
fist-kb init /absolute/path/to/project --purpose "What this KB is for" --owner "Owner name"
fist-kb --kb /absolute/path/to/project/KnowledgeBase admin validate
```

Initialization creates `KnowledgeBase` and refuses an existing KB. The KB is bound to its parent folder (`core.binding.root`): a copied or moved KB refuses to load instead of silently rebinding. For project evidence, put the KB directly under the actual project root. A separate store has its own parent as the evidence boundary; setup requires explicit acknowledgement and never expands source access. It does not import conversations. `FIST_KB_ROOT` can supply the same path when `--kb` is omitted; it names the KB directory, not its parent.

| File | Purpose |
| --- | --- |
| `core.json` | Orientation (purpose, owner, current task, pins, routes), revision and the hashes of the other state files. Written last: the commit marker. |
| `records.jsonl` | Current records, one per line, with a closed field set per kind. |
| `journal.jsonl` | Log entries, archived values (history) and one entry per committed write. |
| `sources.json` | Source catalog. |
| `.pending/` | Unconfirmed previews; cleared by any commit. |
| `prepared/` | Prepared source evidence, one JSONL file per prepared source. |

There are no request or transaction files. Do not edit state files by hand: the hashes in core detect it, and every call then reports `recovery_required`.

### Upgrade a KB from Fist KB 0.3.x

Fist KB 0.4.0 uses a new data layout and reads a 0.3.x KB only through a one-time migration. `kb_status` and the session-start hook report such a KB with `v07_kb_requires_migration`. With the user's consent, plugin setup migrates it (`kb_setup` with mode `migrate` and `by`, your name) into a new sibling folder, `KnowledgeBase.v08`, and selects that folder. The parent stays the same, so the evidence boundary does not change. The old KB is only read and stays as it was; remove or archive it yourself once you no longer need it.

From the CLI, measure first and then migrate into a new, empty folder:

```text
mkdir /absolute/path/to/project/KnowledgeBase.v08
fist-kb --kb /absolute/path/to/project/KnowledgeBase.v08 admin migrate-v07 --from /absolute/path/to/project/KnowledgeBase --by "Your name" --dry-run
fist-kb --kb /absolute/path/to/project/KnowledgeBase.v08 admin migrate-v07 --from /absolute/path/to/project/KnowledgeBase --by "Your name"
```

Every record keeps its ID. Known fields map to the new fields; tags and routes become topics (route order becomes the route's `lead`); note arrays become dated log entries; other fields become labelled log entries ("imported field NAME: ..."). Statuses the new kinds do not allow are remapped and the original is logged. One migration record keeps the old core and catalog values verbatim. Before anything is published, every old field is rebuilt from where it was put and compared type-exactly; any difference publishes nothing. The report lists `lossless`, `spill_rate`, `status_remapped`, `navigation_changes`, `over_limit_warnings` and `task_candidates`. Afterwards, create a `task` record if the work has one and set `core.current_task`.

## Orientation in context

`kb_read({})` returns the orientation. It is one page of about 6,000 estimated tokens by default, with core and the current task first; when `complete` is false, continue as below. Read it again only after context loss, after switching KBs, or when a result shows a revision your own writes do not explain (another writer). Your own write returns the new revision and echoes what it stored.

### Session-start hook

The Windows plugin runs `scripts/session-context.ps1` when a session starts, resumes, is cleared and after compaction (Claude Code `hooks/hooks.json`; Codex `hooks/codex-hooks.json`, after the user trusts it). It reads the project's `.fist-kb.json` and prints the orientation with the project, KB root and revision. Hosts add that text to the model context. It only reads, prints nothing for a project without a selection, and one line for an unusable selection or a KB that still needs migration. Output stays within 6,000 bytes, below both hosts' hook-output caps: the hook asks for a smaller orientation page when needed and, if that page is not complete, says so and includes its continue token. The hook uses the runtime the MCP launcher prepared and never downloads anything, so it adds nothing until the server has started once. The same text is available from the CLI:

```text
fist-kb session-context --project /absolute/path/to/project
```

## Read

`kb_read` takes one JSON object; the keys present choose the mode. From the CLI pass JSON text, `@file` or `-` for stdin:

```text
fist-kb --kb /absolute/path/to/project/KnowledgeBase read
fist-kb --kb /absolute/path/to/project/KnowledgeBase read '{"topic": "startup_checks"}'
fist-kb --kb /absolute/path/to/project/KnowledgeBase read '{"query": "SAMPLE_MODE startup"}'
fist-kb --kb /absolute/path/to/project/KnowledgeBase read '{"ids": ["FACT-0001"], "log": "all", "history": true}'
```

| Request | Returns |
| --- | --- |
| `{}` | Orientation: full core; current task essentials (title, status, step, next, stop, rule titles), or a hint when no task is set; recent writes; index (counts, topics, open questions, active tasks); the task's other fields and last 3 log entries; pinned records |
| `{"ids": [...], "fields"?: [...], "log"?: N or "all" or {"from": n, "to": m}, "history"?: true or [fields]}` | Current fields of each record, then its log and history entries if asked. Unknown IDs are listed in `missing` with the nearest IDs |
| `{"topic": "..."}` | Route members: full current fields for the first records, summaries after half a page |
| `{"query": "...", "topic"?: "...", "scope"?: "current" or "all"}` | Search over current fields and log entries (`all` adds history), at most 3 matching places per record with snippets, plus matching sources and prepared evidence. Records whose recorded status is superseded, historical or cancelled rank at half weight (disputed at three quarters) and name `superseded_by`. A miss names the scope searched |
| `{"kinds": [...], "status"?: [...]}` | Filtered list of summaries |
| `{"changes_since": REV}` | The writes after that revision, with their notes and operations |
| `{"request": "ID"}` | Whether a write with that `request_id` was committed |

Common keys: `limit` (records, default 25) and `budget` (estimated tokens per page, 600 to 30,000; default 6,000 for orientation, 4,000 otherwise). Estimates are compact UTF-8 JSON bytes divided by 3, not a particular model's tokenizer.

Every result has `ok`, `revision`, `mode`, `results`, `page`, `complete` and `continue`. A read never fails because something is large: a record that does not fit a page is split into fields, and a long value into numbered chunks. When `complete` is false, send `{"continue": TOKEN}` alone for the next page. The token carries the request and the revision; if the KB changed in between, the answer is `revision_changed` and you read again.

Search is lexical: try the specific name or configuration and useful alternate wording. A negative result covers only the scope it names; before saying the KB lacks something, search for the specific name or phenomenon, with `"scope": "all"` when older wording matters. Rank, recency and status do not establish authority or truth. Rule titles in orientation are navigation: read the decisions with `ids` before relying on them.

## Records

Every record has `id` (allocated by the tool), `kind`, `status`, `title` (at most 200 B) and `statement` (at most 3,000 B, the current answer). Optional for every kind: `topics` (route membership and tags), `related` (record IDs), `sources` (source IDs), `origin` (where the content came from: `type` plus optional `by`, `at`, `channel`, `reason`) and `resolution` (`{"note": ..., "ids": [...]}`, at most 1,000 B). Automatic: `created_at`, `created_by` (the writer who stored it), `updated_at`, `created_rev`, `updated_rev`, `superseded_by`, `supersedes`.

| Kind | Prefix | Statuses | Extra fields |
| --- | --- | --- | --- |
| `owner_decision` | DEC | active, superseded, historical | quote, action, applicability, limits |
| `observed_fact` | FACT | active, disputed, superseded, historical | evidence, limits, applicability, action |
| `source_claim` | CLAIM | active, disputed, superseded, historical | claimant, evidence, limits |
| `inference` | INF | provisional, active, disputed, superseded | basis, limits |
| `assumption` | ASM | provisional, active, superseded | limits |
| `recommendation` | REC | active, provisional, superseded, historical | applicability, action, limits |
| `open_question` | Q | open, resolved, historical | options, action, limits |
| `task` | TASK | active, awaiting_owner, blocked, paused, completed, cancelled | step, next, stop, rules, limits |

Text fields are at most 2,000 B, `quote` at most 12,000 B (default reads show the first 1,500 B and mark it clipped), log entries at most 4,000 B, ID lists at most 50 (`rules` 20), `topics` at most 20 names of lowercase letters, digits, `_ . -`. Origin types: owner_message, observation, source, inference, peer_message, import. A field the kind does not have is refused with a hint; put such detail in a log entry or a linked record. Migrated records may hold over-limit text; validation warns, and the next `set` of that field must fit.

**Log and history.** A record's log holds dated notes; add one with the `log` operation. History is automatic: every value replaced by `set`, `remove`, `restore` or `status` becomes a history entry and can be put back with `restore`. Plain reads show `log_count` and `history_count`; ask for the entries explicitly.

**Task records.** `step` is what is being done, `next` the next action, `stop` the review or stop boundary, `rules` the decisions that bind the task, `limits` budget and scope. Orientation shows the current task first. Keep one task record per assigned task and point `core.current_task` at the one in progress.

**Routes.** A route is the set of records that carry its topic. Core may describe a route (`about`, at most 200 B) and give an ordered `lead` (at most 10 IDs) that comes first; the other members follow by kind and recency.

Preserve facts, owner decisions, source claims, inferences, assumptions, recommendations and uncertainty as distinct kinds. `origin` says where the content came from (who said or produced it, when, through which channel); it is not the writer or the write time unless the writer is the source. Schema validity, recency and rank never establish truth or supersession; use the `status` operation for that.

## Write

`kb_write` takes one JSON object. From the CLI pass JSON text, `@file` or `-` for stdin. In MCP pass the object as `request`.

```text
{"changes": [ ... ], "by": "Your name", "note": "why", "expect_revision": 41, "preview": false, "request_id": "optional-token"}
```

| Operation | Example | Effect |
| --- | --- | --- |
| `new` | `{"new": {"ref": "d", "kind": "owner_decision", "title": "...", "statement": "...", "quote": "...", "topics": ["release"]}}` | Creates a record with an allocated ID; later operations refer to it as `"$d"` |
| `set` | `{"set": {"id": "TASK-0001", "fields": {"next": "...", "step": "..."}}}` | Replaces fields; `null` removes one; old values go to history |
| `log` | `{"log": {"id": "TASK-0001", "text": "...", "origin": {...}}}` | Appends a log entry; `origin` is optional |
| `add`, `remove` | `{"add": {"id": "FACT-0002", "field": "topics", "items": ["environment"]}}` | Adds or removes list items; removed lists go to history |
| `status` | `{"status": {"id": "REC-0001", "status": "superseded", "by": "REC-0002", "reason": "..."}}` | Changes status; `superseded` needs `by` and links both records; a reason becomes a log entry |
| `restore` | `{"restore": {"id": "FACT-0002", "history": 7}}` | Puts an archived value back; the current one is archived |
| `core` | `{"core": {"current_task": "$t"}}`, `{"core": {"pin": ["DEC-0002"]}}`, `{"core": {"unpin": [...]}}`, `{"core": {"route": {"topic": "release", "about": "...", "lead": ["$d"]}}}`, `{"core": {"remove_route": "old"}}`, `{"core": {"purpose": "..."}}`, `{"core": {"owner": "..."}}`, `{"core": {"guest_reads": false}}` | Named core edits; one key per operation |
| `source` | `{"source": {"ref": "s", "locator": {"type": "file", "path": "/absolute/path/to/project/data.csv"}, "title": "..."}}` | Registers a source (the tool hashes a file); cite it as `"$s"` |
| `source` (revision) | `{"source": {"ref": "s2", "revises": "SRC-0001", "reason": "...", "record_ids": ["DEC-0003"], "git_commit": "FULL_COMMIT"}}` or `history_unavailable_reason` instead of `git_commit` | After an authorized edit of a registered file: a new version at the same path with the current hash; the old version keeps its identity and records the supersession |

All operations in one call commit together as one revision, or none do. `by` names the writer; the CLI also reads `KB_ACTOR`.

**Revisions.** Additive operations (`new`, `log`, `add`, `source`) need no revision. Replacing operations (`set`, `remove`, `restore`, `status`, `core`) need `expect_revision`, the revision of the read or write result the change is based on, never simply the latest. A target record (or core field) changed after that revision is refused as `stale_target` with what changed; unrelated writes since then do not block.

**Result.** `committed` returns the new `revision`, allocated `ids` (`{"d": "DEC-0002"}`) and one echo per change: short values whole, a span diff of long text, and for replaced values their history number and size. Read the echo and correct a mistake with another write before anything relies on it. `preview: true` returns the same echo with `outcome: "preview"` and a `preview_id`; `{"confirm": PREVIEW_ID, "by": ...}` commits exactly that candidate, or refuses if the KB changed. A `request_id` is bound to its operations, note and writer: repeating the same request returns the first outcome without writing; the same ID with different content is refused.

### Example 1: save a new finding

These examples form a **synthetic walkthrough** on an empty disposable KB. Their facts are invented. Replace all content and IDs with real, supported material for actual use.

```json
{
  "by": "Example maintainer",
  "note": "Synthetic startup observation",
  "changes": [
    {"new": {"ref": "f", "kind": "observed_fact", "title": "Local startup check",
             "statement": "The example service passed the local startup check.",
             "limits": "Only startup was checked; deployment was not tested.",
             "topics": ["startup_checks"]}}
  ]
}
```

The result is `committed` with `"ids": {"f": "FACT-0001"}` and the stored values. The topic `startup_checks` now lists the record: `kb_read({"topic": "startup_checks"})`.

### Example 2: correct an existing record

A later check established that startup required sample mode. Read `FACT-0001` first and note the revision of that result (here 2). Replace the current answer, keep the reason in the log, and summarize the correction in `resolution`:

```json
{
  "by": "Example maintainer",
  "note": "Synthetic correction: preserve the startup condition",
  "expect_revision": 2,
  "changes": [
    {"set": {"id": "FACT-0001", "fields": {
      "statement": "The example service starts with SAMPLE_MODE=1; default startup fails because configuration is missing.",
      "resolution": {"note": "Startup success requires SAMPLE_MODE=1. Deployment remains untested."}}}},
    {"log": {"id": "FACT-0001", "text": "A later check showed the successful run used SAMPLE_MODE=1."}}
  ]
}
```

The old statement goes to history automatically (`kb_read({"ids": ["FACT-0001"], "history": true})`), and the old lookup reaches the corrected answer. When a separate record replaces another, create it and mark the old one with `{"status": {"id": "FACT-0001", "status": "superseded", "by": "$new", "reason": "..."}}`, which links both records. Do not infer supersession merely from a later timestamp.

### Example 3: describe a route and pin a decision

```json
{
  "by": "Example maintainer",
  "note": "Make startup findings easy to find",
  "expect_revision": 3,
  "changes": [
    {"core": {"route": {"topic": "startup_checks", "about": "How the service starts and what it needs", "lead": ["FACT-0001"]}}},
    {"add": {"id": "FACT-0001", "field": "topics", "items": ["configuration"]}}
  ]
}
```

A route needs no definition to exist: records carrying a topic form it. Core only adds the description and the `lead` order. `{"core": {"pin": ["DEC-0001"]}}` keeps a record in every orientation; pins are at most 12.

## Outcomes and recovery

Every write is one commit. It stages every changed file on the same volume, checks that the KB did not change in between, replaces the state files and then `core.json` last, reloads and validates.

| Outcome | Meaning and what to do |
| --- | --- |
| `committed` | Committed and verified |
| `not_written` | Nothing was published; fix the request and send it again |
| `committed_unverified` | Core was replaced but verification failed: stop writing and report |
| `uncertain` | Some files were replaced, or a replacement cannot be established: do not resend; check `kb_read({"request": ID})` when you sent a `request_id`, otherwise the orientation, and report |

Non-committed outcomes carry `detail.write_outcome` with the stage, replaced files and cleanup errors. Broken pipes, timeouts or missing output establish neither success nor failure. Never retry a failed write automatically. The public CLI serializes writes with an exclusive `.write.lock` in the KB folder; a busy writer is reported as `write_locked` without automatic retry. Termination may leave a lock behind: check its process and the committed state before removing a confirmed stale lock. A commit may be complete even if lock cleanup failed; that result keeps the engine's outcome and adds `cli_diagnostics`; do not replay it.

## Errors

Errors come as `{"ok": false, "outcome": "not_written", "error": {"code", "message", "hint", ...}}` for writes and `{"ok": false, "error": {...}}` otherwise.

| Code | Meaning and what to do |
| --- | --- |
| `request_invalid`, `operation_invalid` | The request or an operation is malformed; the hint shows the shape |
| `kind_invalid`, `status_not_allowed` | Not a kind, or not a status of that kind; the hint lists the allowed ones |
| `field_not_allowed` | The kind has no such field; use a log entry or a linked record |
| `field_protected` | Set by the tool or by the `status` operation |
| `field_too_large` | Over a limit; keep the current answer short and move detail to the log or a linked record |
| `value_invalid` | Wrong type, empty text, unknown ID in a list, bad locator or a file outside the evidence boundary |
| `unknown_id`, `unknown_ref`, `ref_invalid` | No such record (with nearest IDs), undefined `$ref`, or a bad `ref` name |
| `history_unknown` | No such history entry for `restore` |
| `duplicate_source` | Already registered; cite the existing source ID |
| `no_change` | Every value is already stored |
| `revision_required`, `stale_target` | Pass the revision your change is based on; re-read the changed target |
| `request_id_conflict` | That `request_id` was committed with different content |
| `preview_unknown`, `preview_stale`, `preview_mismatch` | Preview again |
| `actor_required` | Name the writer (`by` or `KB_ACTOR`) |
| `tools_unverified` | Installed package files differ from the package manifest; writes are refused until the reviewed package is reinstalled |
| `revision_changed` | The KB changed between pages; read again |
| `concurrent_write` | Another writer changed the KB during this write; nothing was written |
| `write_locked` | Another public write holds the KB's `.write.lock`; nothing was started |
| `recovery_required`, `invalid_state`, `verification_failed` | State does not match core or does not validate; do not write; run `fist-kb --kb KB admin recover` and report |
| `reader_dependency_changed`, `reader_unavailable`, `guest_access_disabled` | Guest reader refusals ([guest reads](EVIDENCE.md#read-another-owners-kb-as-a-guest)) |
| `missing_state`, `invalid_json`, `invalid_encoding`, `invalid_schema`, `io_error`, `internal_error` | The KB files or the installation are damaged or unreadable; report |
| `prepare_failed` | Preparation refused or failed: unknown or non-file source, changed original, existing different preparation without `--upgrade`, oversized item |
| `already_initialized`, `old_kb_missing`, `old_kb_overlap`, `old_kb_inconsistent`, `old_kb_unsupported` | `init` and `migrate-v07` refusals |
| `v07_kb_requires_migration` | The selected KB uses the Fist KB 0.3.x layout; [migrate it once](#upgrade-a-kb-from-fist-kb-03x) |
| `storage_scope_confirmation_required` | Setup needs the user's acceptance of a separate store's evidence boundary |
| `invalid_request`, `invocation_failed`, `initialization_failed` | A tool or CLI argument is missing or invalid, or no KB is selected |
| `execution_timeout`, `launch_failed`, `invalid_tool_output` | The engine process did not finish or answer; a write may have completed, so read before sending anything again |

## Admin commands

`fist-kb --kb KB admin COMMAND`:

| Command | Effect |
| --- | --- |
| `validate [--deep]` | Full validation with warnings; `--deep` re-hashes file sources and prepared evidence |
| `verify-tools` | Compares the installed package files with the package manifest |
| `migrate-v07 --from OLD_KNOWLEDGEBASE --by NAME [--dry-run]` | Migrates a 0.3.x KB into this new, empty KB folder; `--dry-run` writes nothing |
| `recover` | Inspection only: recorded and actual hashes, staging files |
| `export` | The whole state as one JSON object |

`fist-kb init PROJECT` creates a KB; `admin init` is its engine step and is not needed directly.

## Sources and existing KBs

Registering a file source hashes it; reads show the registration hash, and `admin validate --deep` re-checks the bytes. Registration does not fetch URLs, and extraction does not guarantee complete images, tables or OCR. Structural validity does not prove a claim true. See [Sources, evidence history and guest reads](EVIDENCE.md) for preparation, source revisions and the guest reader.

Back up existing knowledge before changing tools. This package migrates only KBs created by Fist KB 0.3.x and other KBs in the same data layout; it does not import other implementations or conversations.
