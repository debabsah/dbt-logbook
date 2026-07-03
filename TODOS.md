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

## ~~CI pull-request comments~~ SHIPPED v0.6 (ci-report command + workflow snippet)
- **What:** a GitHub Action (GitLab later) that comments on PRs: changed models (checksum diff vs last-good state), impacted downstream models, failed/new tests, duration regressions, and a safe/risky-to-merge summary with a link to run details.
- **Why:** the most commercially legible feature in the operations-layer positioning; removes the most annoying dbt Core CI pain (storing, retrieving, comparing the right state artifacts).
- **Pros:** builds ENTIRELY on shipped surfaces (diff_runs, find_regressions, what_broke, /api/state); no new premises, no new deps.
- **Cons:** CI platform surface area (Actions semantics, PR permissions, comment dedup).
- **Context:** accepted from an outside product review (2026-07-03) as the strongest aligned addition.
- **Blocked by:** launch feedback confirming CI is the top ask.

## Cost-aware operations (three tiers; owner-approved direction 2026-07-03)
The instinct "cost-aware compiler" resolved to cost-aware OPERATIONS: cost is
one more per-node time series, so every shipped mechanism (history, regression
detection, diff, PR reporting, MCP) applies to dollars. Explicitly NOT a SQL
rewriter/optimizer: that requires per-dialect SQL comprehension (Fusion's and
SQLMesh's decade-long fight), inverts our observe-only trust model, and the
warehouse optimizer already does it better.

- **Tier 1 - SHIPPED v0.6:** duration x configurable
  cost-rate per env = estimated compute cost for ANY warehouse/lakehouse
  (labeled "estimated"); exact bytes_billed/bytes_processed where
  adapter_response provides it (BigQuery - already stored). Health screen
  spend view + find_cost_regressions MCP tool.
- **Tier 2 - SHIPPED v0.6 (ci-report; BigQuery dry-run deferred to Tier 2.5, demand-gated):** impacted
  models' historical cost at current schedule + BigQuery dry-run scan delta
  for changed models (free, nothing executes). Cost awareness at merge time,
  where the decision happens.
- **Tier 3 - DEMAND-GATED (credentials):** query-history joins (Snowflake
  QUERY_HISTORY, BQ jobs, Databricks) for exact attribution; cost-aware
  scheduling ADVICE ("hourly schedule on a daily-changing source burns
  $N/mo"). Advice only - automatic data-aware skipping stays declared
  unreachable from artifacts; no dishonest claims.
- **Positioning clause when Tier 1 ships:** "watches your runs and your
  spend.

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

