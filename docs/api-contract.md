# The dbt-logbook metadata contract (stable from v0.2)

Two surfaces expose the run history. Both are views over the same queries
module, so they cannot drift. These are the STABLE public contract: fields may
be added in any release; existing fields/tools are only removed or renamed in
a major version.

Direct access to `.dbtlogbook/history.db` is possible (it's SQLite) but
BEST-EFFORT: the table layout may change in any release, with forward
migrations. If you build on dbt-logbook, build on the API or MCP tools.

## MCP server

```
dbt-logbook mcp        # stdio, run from inside the dbt project
```

| Tool | Answers |
|---|---|
| `get_run_history(limit, env)` | recent runs: status, failures, duration, env |
| `what_broke(runs_back)` | failures in the latest run(s); each flagged `newly_broken` if it passed the previous time it ran |
| `get_model_history(model, limit)` | one node's status + duration across runs (accepts bare name or unique_id) |
| `find_regressions(factor, window, min_seconds)` | models whose latest duration >= factor x median of prior runs |
| `find_flaky_nodes(window, min_flips)` | nodes whose pass/fail flipped repeatedly |
| `diff_runs(run_a, run_b)` | added/removed/modified nodes between two runs (dbt per-node checksums) |
| `what_changed()` | diff of the latest run vs the one before |
| `get_cost_summary(window)` | per-model spend: runtime share, $ estimates (configured rate), exact bytes where the adapter reports them |
| `state_modified_preview(env, dbt_executable)` | what `--select state:modified` would rebuild vs the last good run of `env` (shells out to `dbt ls`; requires dbt on PATH) |

## REST API

Served by `dbt-logbook ui` (localhost only).

| Endpoint | Same data as |
|---|---|
| `GET /api/runs?limit&offset&env` | `get_run_history` |
| `GET /api/runs/{invocation_id}` | one run + per-node results |
| `GET /api/models/{unique_id}` | node info + history |
| `GET /api/models/{unique_id}/sql` | raw/compiled SQL from the latest manifest |
| `GET /api/what-broke?runs_back` | `what_broke` |
| `GET /api/regressions?factor&window&min_seconds` | `find_regressions` |
| `GET /api/flaky?window&min_flips` | `find_flaky_nodes` |
| `GET /api/diff?a&b` | `diff_runs` |
| `GET /api/dag?node&hops&tests` | lineage graph / neighborhood |
| `GET /api/summary` | store totals + last run |
| `GET /api/state/{env}/manifest.json` | last-good manifest for state-based CI (`--defer --state`) |
| `GET /api/freshness?snapshots` | per-source freshness status series over time |
| `GET /api/cost?window&rate` | per-node spend: runtime share, est. cost (rate), exact bytes where reported |
| `GET /docs-site/` | generated `dbt docs` output, when present in the target dir |

## Auth

Localhost binds are open. Binding beyond localhost requires a token
(`--token` / `DBT_LOGBOOK_TOKEN`); every `/api/*` request must then send
`Authorization: Bearer <token>`. The static UI shell stays reachable.

## Semantics (shared definitions)

- **failure**: node status in `error`, `fail`, `runtime error` (case-insensitive)
- **newly broken**: failed now, passed on its previous execution
- **regression**: latest duration >= `factor` x median of previous durations
  within `window`, and latest >= `min_seconds` (default 1s, filters noise)
- **flaky**: pass/fail status flipped >= `min_flips` times within `window`
  runs (checksum-blind: a code fix that repairs a node counts as one flip)
- **diff**: keyed on dbt's own per-node `checksum`; across a dbt major-version
  boundary checksums are not comparable (`engine_changed: true` is set)
