# AB engine canonical deployment

`篩選邏輯/` in this git repository is the only editable source for the AB
engine. Its runtime target is `/opt/mls-screen` on `ssh mls` (port 8002).

- Deploy the main site with `deploy_vps.sh`. It deliberately excludes
  `篩選邏輯/` and only updates `/opt/mls-intraday` (port 8000).
- Deploy the AB engine with `deploy_screen_vps.sh`. It checks the VPS source
  manifest before upload, compiles Python, restarts 8002, waits for HTTP
  readiness, then records a new manifest.
- If an emergency edit was made directly on the VPS, run
  `pull_screen_vps.sh`, review `git diff`, and commit it before the next deploy.
  Never overwrite unexplained VPS drift.
- Runtime databases and backups (`*.db*`, `*.bak*`) are never synchronized.

The 8003 `mls-v4` database remains independent in its Docker volume and is not
part of either deployment path.
