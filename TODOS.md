# Roadmap notes

What's deferred and why. Items marked "demand-gated" ship when real usage
asks for them - open an issue if one of these is blocking you.

## Windows `exec` support (demand-gated)
`exec` relies on POSIX signal semantics (SIGINT forwarding, process groups)
and is documented as unsupported on Windows. `ui`, `import`, `demo`, and
`mcp` are pure Python and should work, but are untested there. Supporting
`exec` needs CTRL_BREAK_EVENT / job-object handling plus a Windows CI leg.
Open an issue if you'd use it.

## UI framework rebuild (deferred)
The UI is deliberately vanilla JS with no build step: at the current screen
count the hash-router + render-function structure is small, has no state-sync
bugs, and keeps contributions easy. Revisit when a screen needs client-side
state shared across views (live updates, persistent filters) or app.js
crosses ~1000 lines.

## Storage engine: SQLite (decided, benchmarked)
Benchmarked at 1000 runs x 30 nodes (30k node_results, roughly 10x a busy
year of hourly runs): the worst query (regression scan) takes 17ms; model
history 0.6ms. SQLite stays - a second engine would add a dependency and an
ops burden to save milliseconds that are invisible over MCP stdio or a local
UI. Revisit only with evidence SQLite is an actual bottleneck.

## Cost visibility: what ships and what doesn't
Cost is treated as one more per-node time series over the store. Deliberately
NOT a SQL rewriter/optimizer: that requires per-dialect SQL comprehension,
inverts the observe-only trust model, and the warehouse optimizer already
does it better.

- Shipped: duration x configured rate for universal estimates; exact
  bytes_processed/bytes_billed where the adapter reports them (BigQuery);
  Health spend view; get_cost_summary MCP tool; per-run cost deltas on
  ci-report regressions.
- Deferred (demand-gated): BigQuery dry-run scan-delta prediction for changed
  models on PRs; warehouse query-history joins (Snowflake QUERY_HISTORY, BQ
  jobs, Databricks) for exact attribution - these need credentials, which
  breaks the zero-config default, so they wait for users who want the trade;
  cost-aware scheduling advice ("this hourly schedule on a daily-changing
  source burns $N/month"). Advice only - automatic data-aware rebuild
  skipping cannot be done correctly from artifacts alone, so it is out of
  scope rather than half-done.

## Team/server mode (demand-gated)
Shared multi-user deployment: users/roles, audit trail, backup tooling,
multi-project. Waits for teams actually running `serve` on shared boxes and
hitting the single-token ceiling. A Postgres backend is explicitly not
planned (see the SQLite note above) - it would end zero-config and double the
storage path.

## Incident workflow (demand-gated)
Acknowledge/mute alerts, owner routing, incident timelines. The primitives
(flaky tracking, failure-to-change attribution via diff) already exist;
workflow features wait for feedback from real `serve` deployments about
alert volume.

## GitLab CI support (demand-gated)
`ci-report` emits plain markdown, so any CI can post it; the documented
workflow is GitHub-first. A GitLab MR example lands when someone asks.
