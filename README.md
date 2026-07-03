# dbt-logbook

Run history for dbt. Every invocation recorded, nothing overwritten.

dbt writes `run_results.json` and overwrites it on the next run. dbt-logbook keeps
every run in a local SQLite store and gives you the views that history makes possible:
a run timeline, per-model duration trends, what-changed diffs between runs, and a
lineage browser - with zero configuration and zero changes to your dbt project.

```
cd your-dbt-project
dbt-logbook ui        # instant read-only UI over the artifacts dbt already wrote
dbt-logbook exec -- dbt build   # wrap your runs; history accrues from here
```

Status: pre-release, under active development. v0.1 scope: local store,
instant UI (timeline, model detail, diff, DAG), exec capture wrapper, demo command.

Works with dbt Core v1 and v2 - dbt-logbook reads only dbt's stable surfaces
(CLI and artifact files), never its internals.

License: Apache-2.0. Not affiliated with dbt Labs; "dbt" is a trademark of dbt Labs, Inc.
