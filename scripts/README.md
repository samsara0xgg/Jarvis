# scripts/

Operational and developer scripts. Not part of the runtime; invoked manually
or by maintenance jobs.

Planned (land as needed):

- `replay_log.py` — replay events from `var/mac_events.db` to verify
  projection determinism
- `rebuild_projection.py` — wipe + rebuild a named projection from scratch
- `backup.sh` — snapshot event log + artifact store
- `migrate.py` thin wrapper — invokes the in-package migration runner from
  `jarvis/state/store/migrations/`
