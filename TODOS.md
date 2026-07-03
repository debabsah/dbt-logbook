# TODOS

## Windows `exec` support
- **What:** CTRL_BREAK_EVENT / job-object process handling for the exec wrapper + a Windows CI leg.
- **Why:** the SQL Server pitch targets a Windows-heavy audience, and `exec` is their history-capture path.
- **Pros:** unlocks the audience the SQL Server claim courts.
- **Cons:** platform-specific process code; CI minutes; zero users until launch proves demand.
- **Context:** v0.1 documents `exec` as unsupported on Windows (POSIX signal semantics). `ui`/`import` are pure Python and likely fine untested. Decision made in /plan-eng-review 2026-07-03 (outside-voice finding #10: "unsupported, not untested").
- **Blocked by:** v0.1 launch + demand signal (Windows users filing issues).

## ~~SQLite vs DuckDB engine evaluation~~ RESOLVED 2026-07-03
Benchmarked at 1000 runs x 30 nodes (30k node_results, ~10x a busy year of
hourly cron): worst MCP-shaped query (regression scan) 17ms; model history
0.6ms; what-broke 0.2ms. SQLite locked as the engine for the v0.2 contract -
DuckDB would add a dependency to save milliseconds invisible over MCP stdio.
