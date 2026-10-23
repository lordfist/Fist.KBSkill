# Using Fist KB

Examples use the installed `fist-kb` CLI. From a source checkout, prefix commands with `uv run --locked`. Plugins expose the same engine through MCP; see [client integration](INTEGRATIONS.md). Paths, IDs, actors, revisions and hashes below are examples: use the actual selected KB and inspected values.

## Everyday memory workflow

For an agent using Fist KB as its working memory:

1. Keep the complete current `core.json` in working context.
2. Consult `kb_context` before answering or acting on every request, including short requests. Use the task's topics, or no topic for general orientation.
3. Read identified records when their detail matters. Search when the brief/routes do not answer the question, when evidence conflicts, and before concluding knowledge is missing.
4. Within authorized maintenance, save material decisions, corrections, findings and outcomes as they occur. Check existing records first; avoid duplicate notes and routine chatter.
5. Verify writes, refresh core and check the affected lookup. After compaction or a new session, reload the [operating skill](../skills/fist-kb/SKILL.md), complete core and relevant records before resuming.

This routine requires the host to supply the skill and the agent to follow it. Installation alone does not continuously inject core or capture conversations. Explicit read-only instructions still prohibit writes.

## Choose the data directory

The plugin's `kb_status(project=..., verify_runtime=true)` checks packaged-runtime integrity during onboarding/recovery and returns the selected `kb_root` and `evidence_root`. Use guided setup if none is selected. A service-ready result does not prove the operating skill is loaded; complete the [working-memory setup](INTEGRATIONS.md#enable-project-memory). Direct CLI initialization uses an existing project directory:

```text
fist-kb init /absolute/path/to/project
fist-kb --kb /absolute/path/to/project/KnowledgeBase validate --compact
```

Initialization creates `KnowledgeBase` and refuses an existing KB. For project evidence, put it directly under the actual project root. A separate store has its own parent as the evidence boundary; setup requires explicit acknowledgement and never expands source access. It does not import conversations or migrate another implementation. `FIST_KB_ROOT` can supply the same path when `--kb` is omitted; it names the KB directory, not its parent.

| File | Purpose |
| --- | --- |
| `core.json` | Compact project orientation, revision, current state and routes. |
| `records.jsonl` | Typed knowledge records and their qualifications/history. |
| `sources.json` | Source identities and locators. |

Requests and drafted transactions belong in `KnowledgeBase/transactions`. Prepared representations and recent-access pointers are separate derived data. Canonical files are updated through transactions, not direct editing.

## Keep the complete core in context

Use MCP `kb_core(project=PROJECT, budget=8000, expect_revision=REV)` to obtain complete core and the actual selected/evidence roots. The revision guard is optional on the first read. Omit project for a fixed connection. Require `core_is_projection:false`; the tool fails explicitly if full core cannot fit. The CLI alternative is:

```text
fist-kb --kb /absolute/path/to/project/KnowledgeBase core --ids-only --limit 1 --budget 8000 --compact
```

The returned `core` is complete and `core_is_projection` must be `false`. Here `--ids-only` and `--limit` reduce accompanying record output, not core. If the tool or host truncates the response, obtain the missing content; a filename or summary is not a complete read.

MCP `kb_context` always returns a brief. Its `orientation` may omit core fields and routes, even with a larger budget. Use `kb_core` for complete core; a larger brief budget does not change the brief into full core.

Load complete core on first use, after session/context loss, after switching KBs, and whenever its revision changes or its contents are no longer retained. Compare every request's context revision with the retained core. Refresh after successful writes; if concurrent work changes the revision during retrieval, refresh core and relevant records together. Keep core small enough to retain; put detailed evidence in records.

## Retrieve before expanding context

```text
fist-kb --kb /absolute/path/to/project/KnowledgeBase context --topic startup_checks --brief --compact
fist-kb --kb /absolute/path/to/project/KnowledgeBase search "startup" --records-only --limit 10 --compact
fist-kb --kb /absolute/path/to/project/KnowledgeBase get FACT-0001 --compact
fist-kb --kb /absolute/path/to/project/KnowledgeBase get FACT-0001 --fields statement uncertainty current_resolution --compact
```

Use exact topic routes and record IDs when known. Search uses lexical matching, so try the specific name/configuration and useful alternate wording. A brief is a projection: inspect `detail_required`, `truncated_fields`, hidden matches and omitted-match pointers, then retrieve material detail. Search before declaring absence; an empty query or first page is insufficient.

Historical fields are excluded from normal matching; use `--history` when old wording matters. Rank, recency and status do not establish authority or truth. A `resolved` owner decision may still apply; inspect its meaning and any current correction.

When `next_offset` is present, continue the same query/scope with the first page's revision:

```text
fist-kb --kb /absolute/path/to/project/KnowledgeBase search "startup" --records-only --limit 10 --offset NEXT_OFFSET --expect-revision REV --compact
```

Restart after a revision change. Estimated token budgets describe output size, not a particular model's tokenizer.

## Write request reference

The MCP `kb_draft` accepts a request object. Its generic dictionary schema does not define the nested fields; use this reference. The CLI consumes the same object from a JSON file.

| Request key | Meaning |
| --- | --- |
| `actor` | Required nonempty name of the actual maintainer. |
| `note` | Reason for this change; supply a meaningful nonempty string. |
| `new_records` | Complete new record objects with unused IDs. |
| `records` | Existing-record edits: each has `id` and `merge`, `add` and/or `append`. |
| `core` | Core/navigation edits using `merge`, `add` and/or `append`. |

Required record fields: `id`, `kind`, `status`, and a nonempty `statement`. Use the matching prefix and four digits, for example `FACT-0001`; inspect existing IDs before allocating one.

| ID prefix | `kind` | Use |
| --- | --- | --- |
| `DEC` | `owner_decision` | An actual owner decision, with its provenance. |
| `FACT` | `observed_fact` | A directly verified observation, with verification scope. |
| `CLAIM` | `source_claim` | A report attributed to a source or another agent. |
| `INF` | `inference` | A conclusion derived from evidence. |
| `ASM` | `assumption` | An explicit unverified premise. |
| `REC` | `recommendation` | A proposal, not an accepted decision. |
| `Q` | `open_question` | An unresolved question. |

Statuses: `active`, `provisional`, `historical`, `superseded`, `disputed`, `open`, `resolved`. Choose by meaning, not age. Preserve origin, applicability, uncertainty and evidence even where the minimal schema permits their omission. `source_refs` and `related_record_ids` must resolve; an empty source list is not verification. Use actual conversation provenance when the source is a user decision rather than an invented source ID.

| Edit operation | Contract |
| --- | --- |
| `merge` | Recursively update **existing** object keys. Unspecified fields survive; scalars and arrays are replaced in full. Unknown keys fail. |
| `add` | Map JSON Pointer paths to new values. The leaf must be absent and its object parents must exist. |
| `append` | Map JSON Pointer paths to nonempty arrays of new items. The target must already be an array. |

Do not overlap edits to the same path or its parent. IDs, automatic timestamps, revision and state hashes are managed/protected. A projected record or partial core is never a complete replacement object.

Read before choosing an operation: add a missing field; merge an existing field; append only to an existing array. Topic routes are objects such as `{"record_ids":["FACT-0001"],"topic_hints":["startup"]}`, not bare ID lists. References can point to records created in the same request.

## Example 1: save a new finding

These examples form a **synthetic walkthrough** on an empty disposable KB. Their facts are invented. Replace all content and IDs with real, supported material for actual use.

```json
{
  "actor": "Example maintainer",
  "note": "Synthetic startup observation",
  "new_records": [
    {
      "id": "FACT-0001",
      "kind": "observed_fact",
      "status": "active",
      "statement": "The example service passed the local startup check.",
      "source_refs": [],
      "origin": {"type": "synthetic_example"},
      "uncertainty": "Only startup was checked; deployment was not tested.",
      "resolution_notes": []
    }
  ]
}
```

In MCP, pass this object directly as `request` to `kb_draft`, with the actual `expect_revision` and `project`. The tool creates request and transaction files under `transactions`; you do not need a separate file-writing tool.

For CLI use, create `KnowledgeBase/transactions` if absent and save the object as `first-request.json`. Draft to a new filename:

```text
fist-kb --kb /absolute/path/to/project/KnowledgeBase status --compact
fist-kb --kb /absolute/path/to/project/KnowledgeBase draft /absolute/path/to/project/KnowledgeBase/transactions/first-request.json --output /absolute/path/to/project/KnowledgeBase/transactions/first-transaction.json --expect-revision REV --compact
```

A draft has not saved canonical knowledge. Complete the review/commit workflow below before the next example.

## Example 2: correct an existing record

Assume a later check established that startup required sample mode. Read `FACT-0001` first. In this walkthrough, `statement` and `uncertainty` exist, `current_resolution` is absent, and `resolution_notes` is an existing array:

```json
{
  "actor": "Example maintainer",
  "note": "Synthetic correction: preserve the startup condition",
  "records": [
    {
      "id": "FACT-0001",
      "merge": {
        "statement": "The example service starts with SAMPLE_MODE=1; default startup fails because configuration is missing.",
        "uncertainty": "Only these local startup configurations were checked; deployment remains untested."
      },
      "add": {
        "/current_resolution": {
          "note": "Startup success requires SAMPLE_MODE=1. Default startup fails; deployment remains untested."
        }
      },
      "append": {
        "/resolution_notes": [
          {
            "note": "Earlier statement: The example service passed the local startup check. The later check established that the successful run used SAMPLE_MODE=1.",
            "origin": {"type": "synthetic_example"}
          }
        ]
      }
    }
  ]
}
```

If `current_resolution` already exists, merge its existing fields instead. If the history array is absent, add it with the first note instead of using append. For actual work, preserve the old wording and the new evidence's provenance rather than copying this invented history.

Draft, preview and commit at the refreshed revision. Read back the record. Keep a concise current answer and useful attributed history; if the correction belongs in a separate record, link it from the older record and update relevant routes so the old lookup reaches it. Do not infer supersession merely from a later timestamp.

## Example 3: create and update a topic route

After the record exists, inspect complete core. If `routes` exists and `startup_checks` does not, add this route:

```json
{
  "actor": "Example maintainer",
  "note": "Make startup findings discoverable by topic",
  "core": {
    "add": {
      "/routes/startup_checks": {
        "record_ids": ["FACT-0001"],
        "topic_hints": ["startup"]
      }
    }
  }
}
```

If the entire `routes` field is absent, add `/routes` with the desired map instead; never replace an existing map with only one route.

Once the route exists, this update changes its existing hints and retains its record IDs and all other routes:

```json
{
  "actor": "Example maintainer",
  "note": "Include the configuration term in existing navigation",
  "core": {
    "merge": {
      "routes": {
        "startup_checks": {
          "topic_hints": ["startup", "SAMPLE_MODE"]
        }
      }
    }
  }
}
```

To change an existing route's `record_ids`, merge the **complete intended array**, preserving every ID that should remain. To add IDs without replacing the existing array, use `core.append` with the path `/routes/startup_checks/record_ids` and the additional existing IDs. Do not append duplicates.

After each commit, reload complete core and run:

```text
fist-kb --kb /absolute/path/to/project/KnowledgeBase context --topic startup_checks --brief --compact
fist-kb --kb /absolute/path/to/project/KnowledgeBase get FACT-0001 --compact
```

Verify that the topic exposes the corrected conditional result and retains the deployment limitation. Check the original lookup after correcting an already-routed record too.

## Review, commit and verify every example

Use the current revision and the actual drafted transaction path returned by the tool. The MCP sequence below uses project mode. For an explicitly fixed MCP connection, omit `project` and use its configured KB root:

1. `kb_draft(request=..., expect_revision=REV, project=PROJECT)`.
2. `kb_preview(transaction=PATH, expect_revision=REV, project=PROJECT)`.
3. Inspect all pages. Continue with returned `next_offset`, `transaction_sha256` and `preview_time`; keep revision, budget and limit unchanged.
4. `kb_apply(transaction=PATH, expect_revision=REV, transaction_sha256=SHA, project=PROJECT)`.
5. Read back affected records, reload complete core, and check changed routes. Report IDs, revision and what was actually retained.

The CLI equivalents for a drafted transaction are:

```text
fist-kb --kb /absolute/path/to/project/KnowledgeBase apply /absolute/path/to/project/KnowledgeBase/transactions/first-transaction.json --dry-run --preview-values --expect-revision REV --compact
fist-kb --kb /absolute/path/to/project/KnowledgeBase apply /absolute/path/to/project/KnowledgeBase/transactions/first-transaction.json --expect-revision REV --expect-transaction-sha256 SHA --compact
fist-kb --kb /absolute/path/to/project/KnowledgeBase get FACT-0001 --compact
```

`REV`, `SHA`, `PATH` and `PROJECT` are placeholders, not literal values. CLI preview continuation also needs `--offset NEXT_OFFSET --expect-transaction-sha256 SHA --preview-time TIME`. Inspect every relevant full-value page. Changed state or transaction bytes require a new review; draft/preview success is not commit success.

Inspect structured errors, `write_outcome` and `cli_diagnostics` before another write. A commit may be complete even if lock cleanup failed; preserve that outcome and do not replay it. Failure or timeout does not prove rollback. The public adapter uses an exclusive `.write.lock`; a busy writer is reported without automatic retry. Termination may leave a lock behind: check its process and committed state before removing a confirmed stale lock. Do not blindly replay an uncertain write.

## Sources and existing KBs

Preview, commit and deep validation may read registered originals/prepared evidence; use them only within the authorized source scope. Registration does not fetch URLs, and extraction does not guarantee complete images, tables or OCR. Structural validity does not prove a claim true.

Back up existing knowledge before changing tools. Compare local fields, commands and output consumers against this release using a copy. This package does not automatically migrate another KB implementation.

See [Sources, evidence history and guest reads](EVIDENCE.md) for the CLI-only registration, preparation, source-revision, checkpoint, guest and runtime-inventory workflows.
