"""Run stage2 with bounded retries and a persistent local failure alert."""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Sequence


DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 300
ALLOWED_ENV_KEYS = {
    "STAGE2_MAX_ATTEMPTS",
    "STAGE2_RETRY_DELAY_SECONDS",
}


def load_env_files(paths: Iterable[Path]) -> None:
    """Load simple KEY=VALUE entries without overriding service environment."""
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key = key.strip()
            if key not in ALLOWED_ENV_KEYS:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def journal_alert(message: str) -> bool:
    print(f"[stage2-alert] {message}", file=sys.stderr, flush=True)
    return True


def _record_status(status: str, message: str, path: Path) -> None:
    payload = {
        "status": status,
        "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "message": message,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def record_failure(message: str, path: Path) -> None:
    _record_status("failed", message, path)


def record_success(path: Path) -> None:
    _record_status("ok", "stage2 completed", path)


def run_pipeline(
    command: Sequence[str],
    *,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry_delay: int = DEFAULT_RETRY_DELAY,
    run_command: Callable[..., object] = subprocess.run,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[str], bool] = journal_alert,
    success_check: Callable[[], bool] | None = None,
) -> int:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delay < 0:
        raise ValueError("retry_delay must not be negative")

    last_rc = 1
    for attempt in range(1, max_attempts + 1):
        print(f"[stage2-retry] attempt={attempt}/{max_attempts}", flush=True)
        result = run_command(list(command))
        last_rc = int(result.returncode)
        if last_rc == 0:
            try:
                complete = success_check is None or bool(success_check())
            except Exception as error:
                print(
                    f"[stage2-retry] completeness check failed:{type(error).__name__}",
                    flush=True,
                )
                complete = False
            if complete:
                print(f"[stage2-retry] success attempt={attempt}", flush=True)
                return 0
            last_rc = 75  # EX_TEMPFAIL: command returned zero but required rows are missing.
        if attempt < max_attempts:
            print(
                f"[stage2-retry] failed exit={last_rc}; retry in {retry_delay}s",
                flush=True,
            )
            sleep(retry_delay)

    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        notify(
            "🚨 MLS stage2 最終失敗\n"
            f"host={socket.gethostname()}\n"
            f"time={now}\n"
            f"已重試 {max_attempts} 次，exit={last_rc}\n"
            "請查看 journalctl -u mls-screen-stage2.service"
        )
    except Exception as error:
        print(
            f"[stage2-alert] alert sink failed:{type(error).__name__}",
            file=sys.stderr,
            flush=True,
        )
    return last_rc


def default_command(app_dir: Path) -> list[str]:
    # Invoke through bash so a lost executable bit cannot become systemd 203/EXEC.
    return ["/usr/bin/bash", str(app_dir / "deploy" / "run_stage2.sh")]


def default_env_files(app_dir: Path) -> tuple[Path, ...]:
    return (
        app_dir / ".env",
        app_dir.parent / ".env",
    )


def tables_complete(
    db_path: Path,
    data_date: str,
    expected_codes: set[str],
    tables: Sequence[str] = ("daily_bar", "inst_flow", "margin"),
) -> tuple[bool, str]:
    expected = {str(code) for code in expected_codes}
    details = []
    all_complete = True
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        for table in tables:
            rows = connection.execute(
                f"SELECT DISTINCT code FROM {table} WHERE data_date=?",
                (data_date,),
            ).fetchall()
            present = {str(row[0]) for row in rows}
            found = len(expected & present)
            details.append(f"{table}={found}/{len(expected)}")
            all_complete = all_complete and expected.issubset(present)
    return all_complete, " ".join(details)


def default_success_check(app_dir: Path) -> bool:
    from phase import Phase, get_phase, resolve_data_date
    import config

    if get_phase() is Phase.CLOSED:
        print("[stage2-retry] market closed; completeness check skipped", flush=True)
        return True
    data_date = resolve_data_date().isoformat()
    ok, detail = tables_complete(app_dir / "mls.db", data_date, set(config.UNIVERSE))
    print(f"[stage2-retry] data_date={data_date} {detail}", flush=True)
    return ok


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        print(f"[stage2-retry] invalid {name}; using {default}", flush=True)
        return default


def main() -> int:
    app_dir = Path(__file__).resolve().parent
    load_env_files(default_env_files(app_dir))
    status_path = app_dir / "stage2-status.json"

    def persistent_alert(message: str) -> bool:
        try:
            record_failure(message, status_path)
        finally:
            journal_alert(message)
        return True

    rc = run_pipeline(
        default_command(app_dir),
        max_attempts=_env_int("STAGE2_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
        retry_delay=_env_int("STAGE2_RETRY_DELAY_SECONDS", DEFAULT_RETRY_DELAY),
        notify=persistent_alert,
        success_check=lambda: default_success_check(app_dir),
    )
    if rc == 0:
        record_success(status_path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
