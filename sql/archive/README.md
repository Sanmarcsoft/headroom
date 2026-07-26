# Archived: anonymous Supabase telemetry-beacon schema

The five files in this directory (`create_proxy_telemetry_v2.sql`,
`create_dashboard_summary.sql`, `upgrade_dashboard_v2.sql`,
`upgrade_telemetry_cache_bust.sql`, `upgrade_telemetry_stack_context.sql`)
defined and evolved the Supabase-hosted `proxy_telemetry_v2` /
`dashboard_summary` tables that the anonymous telemetry beacon used to write
to, including `anon` role `INSERT`/`SELECT` grants and public read policies.

The beacon itself (the code in `headroom/telemetry/beacon.py` that POSTed
aggregate stats to that hardcoded Supabase endpoint) was removed in commit
`53be64ca128cb1906b0e508a8343c91fed1f47fd` ("chore(telemetry): remove
Supabase anonymous beacon; fix contact domain to headroomlabs.ai (#1526)").
That commit did not touch `sql/`, so these schema files were left behind,
still describing a public anonymous-write table that nothing in this
codebase writes to anymore.

Nothing in `headroom/` references these files. They are kept here purely as
a historical record of the schema that existed prior to #1526, in case the
Supabase project itself still needs to be decommissioned by hand. Do not run
these against a live database expecting them to support current telemetry:
current telemetry (`HEADROOM_TELEMETRY`) is local-only and sends nothing
externally; see `headroom/telemetry/beacon.py`.
