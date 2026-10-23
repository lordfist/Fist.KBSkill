# Changelog

## 0.2.2

- Add complete, bounded, revision-aware core retrieval through MCP.
- Make setup load the working skill and relevant knowledge even for existing bindings; offer a scoped, optional persistent project-memory entry.
- Explain and require acknowledgement of separate-store evidence boundaries.
- Make fixed-mode status and setup consistent with the fixed KB.
- Preserve bounded CLI cleanup diagnostics and completed-write outcomes at the API/MCP boundary.
- Add runtime verification to status and a discoverable CLI evidence/guest reference.

## 0.2.1

- Clarify the public introduction and getting-started documentation.
- Require KB consultation for every request and complete core retention/recovery.
- Explain when to read, search, save and maintain corrections.
- Add verified examples for record creation, correction and topic-route updates.
- Repackage the shared plugin and Python wheel; the KB engine is unchanged.

## 0.2.0

- Start the plugin before a KB exists; guided setup selects or creates it.
- Remember one explicit selection per project in a small local file.
- Add a Windows folder picker and private uv/runtime preparation.
- Preserve direct CLI and fixed-MCP bindings, reviewed writes and existing data.
- Refuse silent adoption of evolved Seed KBs.
- Keep one source tree and one shared plugin package; remove expanded per-client copies and the redundant source archive.

## 0.1.0

Initial public package candidate: standalone Python CLI/API, local MCP, thin client wrappers, typed records and reviewed transactions. Provenance is retained in PROVENANCE.json.
