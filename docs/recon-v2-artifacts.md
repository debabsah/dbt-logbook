# Recon: dbt Core v2 artifact compatibility (2026-07-03)

Method: ran `dbt build` on jaffle_shop_duckdb with dbt Core 1.11.12 (dbt-duckdb 1.10.1)
and dbt Core 2.0.0-alpha.3 (Rust engine, bundled DuckDB adapter, ADBC driver
auto-downloaded). Captured and diffed `target/` artifacts. Fixtures preserved in
`tests/fixtures/dbt-1.11/` and `tests/fixtures/dbt-2.0/`.

## Findings

1. v2 still writes `manifest.json` and `run_results.json` as JSON in `target/`,
   with the SAME schema versions as 1.11: manifest v12, run_results v6.
   Parquet artifacts appear in `target/data/` in addition, not as a replacement.
   Consequence: pure-JSON ingest covers v1 and v2; no pyarrow dependency needed.

2. Fields dbt-logbook extracts, verified present in both:
   - manifest nodes: `checksum` (sha256), `depends_on`, `resource_type`, `name`,
     `alias`, `config`, `compiled_code` path fields
   - run_results results: `unique_id`, `status`, `execution_time`, `timing`,
     `message`, `adapter_response`
   - run_results metadata: `invocation_id`, `generated_at`, `dbt_version`

3. v2 removes node keys: `created_at` (the volatility offender), `build_path`,
   `deprecation_date`, `doc_blocks`, `docs`, `extra_ctes`, `extra_ctes_injected`,
   `group`, `time_spine`. Adds: `classifiers`.
   v2 removes result keys: `batch_results`, `compiled`, `compiled_code`, `failures`.
   Consequence: `failures` extraction must be optional (fall back to status/message);
   tolerant extraction handles all removals by design.

4. `run_results.args.target` was None on the v1 run (default target). Env identity
   must fall back: `args.target` -> profile default -> `--env` override -> "default".

5. Caveat: the same model file hashes to DIFFERENT checksums under v1 vs v2 engines.
   A v1 -> v2 upgrade produces a one-time "everything modified" diff. Document in
   the diff screen ("engine changed between these runs").

## Matrix note

dbt 1.11 exists (current stable) - fixture matrix is 1.7 / 1.8 / 1.10 / 1.11 / 2.0.
All five fixture sets now live in tests/fixtures/ and the ingest suite runs
against each (1.7 exercises the older manifest v11 / run_results v5 schemas;
generated from a period-appropriate jaffle-shop checkout since current
jaffle-shop uses test syntax that dbt 1.7/1.8 predate).
