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

## UI framework rebuild (deferred at v0.5)
- **What:** vanilla JS -> a component framework (the design doc scheduled this "as screen count grows").
- **Why deferred:** at 6 views the hash-router + render-function structure is ~600 lines, has no state-sync bugs, and keeps the no-build-step property that makes contributions easy. The ceiling hasn't hurt yet.
- **Revisit when:** a screen needs client-side state shared across views (live updates, filters that persist), or app.js crosses ~1000 lines.

## CI pull-request comments (leading v0.6 candidate)
- **What:** a GitHub Action (GitLab later) that comments on PRs: changed models (checksum diff vs last-good state), impacted downstream models, failed/new tests, duration regressions, and a safe/risky-to-merge summary with a link to run details.
- **Why:** the most commercially legible feature in the operations-layer positioning; removes the most annoying dbt Core CI pain (storing, retrieving, comparing the right state artifacts).
- **Pros:** builds ENTIRELY on shipped surfaces (diff_runs, find_regressions, what_broke, /api/state); no new premises, no new deps.
- **Cons:** CI platform surface area (Actions semantics, PR permissions, comment dedup).
- **Context:** accepted from an outside product review (2026-07-03) as the strongest aligned addition.
- **Blocked by:** launch feedback confirming CI is the top ask.

## Credential-free cost/volume signals from adapter_response
- **What:** extract bytes_processed/bytes_billed (BigQuery) and similar per-node stats already present in run_results adapter_response; show cost/volume trends on the Health screen + an MCP tool (find_models_with_cost_spike).
- **Why:** cost visibility with ZERO warehouse credentials - the fraction of "cost intelligence" that fits the zero-config premise.
- **Pros:** the data is already in the store (adapter_response is ingested today, rows_affected already extracted); pure extraction + view work.
- **Cons:** coverage varies by adapter (rich on BigQuery, thin elsewhere); must not overpromise "cost" where only rows/timing exist.
- **Context:** the keepable 20% of the outside review's cost-intelligence proposal. The other 80% (warehouse query-history integrations) is demand-gated below.
- **Blocked by:** nothing - cheap add whenever.

## Team/server mode (DEMAND-GATED)
- **What:** shared multi-user deployment: users/roles, audit trail, backup tooling, possibly multi-project.
- **Trigger:** repeated issues from teams actually running `serve` on shared boxes and hitting the single-token ceiling.
- **Explicitly rejected for now:** a Postgres backend. SQLite is locked into the v0.2 contract by benchmark (worst query 17ms at 10x scale); a second engine kills zero-config and doubles the storage path forever. Revisit only with evidence SQLite is the actual bottleneck.

## Warehouse query-history cost integrations (DEMAND-GATED)
- **What:** map dbt nodes to Snowflake/BigQuery/Databricks query history for real cost-per-model.
- **Trigger:** users asking for cost attribution beyond adapter_response signals, and willing to provide read-only warehouse credentials.
- **Why gated:** requires credentials + a per-warehouse integration treadmill - breaks the zero-config premise that every review round confirmed as the differentiator.

## Incident workflow layer (DEMAND-GATED)
- **What:** acknowledge/mute alerts, owner routing, incident timelines, postmortem generation.
- **Trigger:** alert volume feedback from real serve deployments (people drowning in webhook noise).
- **Why gated:** building incident management before anyone receives alerts is PagerDuty-lite cosplay. The primitives (flaky tracking, failure-to-change attribution via diff) already exist.

## Considered and rejected
- **Semantic-layer reader** (parse/validate metrics YAML): YAGNI; duplicates dbt's own parsing; premise-6 territory.

