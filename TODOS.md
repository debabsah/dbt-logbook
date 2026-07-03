# TODOS

## Windows `exec` support
- **What:** CTRL_BREAK_EVENT / job-object process handling for the exec wrapper + a Windows CI leg.
- **Why:** the SQL Server pitch targets a Windows-heavy audience, and `exec` is their history-capture path.
- **Pros:** unlocks the audience the SQL Server claim courts.
- **Cons:** platform-specific process code; CI minutes; zero users until launch proves demand.
- **Context:** v0.1 documents `exec` as unsupported on Windows (POSIX signal semantics). `ui`/`import` are pure Python and likely fine untested. Decision made in /plan-eng-review 2026-07-03 (outside-voice finding #10: "unsupported, not untested").
- **Blocked by:** v0.1 launch + demand signal (Windows users filing issues).

## SQLite → DuckDB engine evaluation (before v0.2 contract freeze)
- **What:** benchmark MCP-shaped analytical queries (cross-run aggregations, sparkline scans) on a realistically sized store; decide SQLite vs DuckDB with data.
- **Why:** the engine decision locks when the v0.2 REST/MCP API contract publishes; after that an engine swap breaks best-effort direct-file consumers.
- **Pros:** last-responsible-moment decision, made with numbers.
- **Cons:** an afternoon that might confirm the obvious.
- **Context:** v0.1 ships stdlib SQLite (zero deps). DuckDB adds a dep but wins on analytical scans. Design doc Open Question #3; eng review pinned the sequencing.
- **Blocked by:** v0.1 store existing with seeded/real data.
