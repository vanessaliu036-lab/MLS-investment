import os
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import stage2_retry


class _Result:
    def __init__(self, returncode):
        self.returncode = returncode


class Stage2RetryTests(unittest.TestCase):
    def test_retries_twice_then_returns_success_without_alert(self):
        run_command = Mock(side_effect=[_Result(7), _Result(7), _Result(0)])
        sleep = Mock()
        notify = Mock()

        rc = stage2_retry.run_pipeline(
            ["/usr/bin/bash", "deploy/run_stage2.sh"],
            max_attempts=3,
            retry_delay=300,
            run_command=run_command,
            sleep=sleep,
            notify=notify,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(run_command.call_count, 3)
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(300)] * 2)
        notify.assert_not_called()

    def test_zero_exit_retries_until_required_data_is_complete(self):
        run_command = Mock(side_effect=[_Result(0), _Result(0), _Result(0)])
        success_check = Mock(side_effect=[False, False, True])
        sleep = Mock()
        notify = Mock()

        rc = stage2_retry.run_pipeline(
            ["/usr/bin/bash", "deploy/run_stage2.sh"],
            max_attempts=3,
            retry_delay=10,
            run_command=run_command,
            sleep=sleep,
            notify=notify,
            success_check=success_check,
        )

        self.assertEqual(rc, 0)
        self.assertEqual(run_command.call_count, 3)
        self.assertEqual(success_check.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        notify.assert_not_called()

    def test_table_completeness_requires_every_expected_code_in_each_source(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "mls.db"
            with sqlite3.connect(db_path) as connection:
                for table in ("daily_bar", "inst_flow", "margin"):
                    connection.execute(f"CREATE TABLE {table} (code TEXT, data_date TEXT)")
                    connection.executemany(
                        f"INSERT INTO {table} VALUES (?, ?)",
                        [("A", "2026-08-13"), ("B", "2026-08-13")],
                    )

            ok, detail = stage2_retry.tables_complete(
                db_path,
                "2026-08-13",
                {"A", "B", "C"},
            )
            self.assertFalse(ok)
            self.assertIn("daily_bar=2/3", detail)
            self.assertIn("inst_flow=2/3", detail)
            self.assertIn("margin=2/3", detail)

            with sqlite3.connect(db_path) as connection:
                for table in ("daily_bar", "inst_flow", "margin"):
                    connection.execute(
                        f"INSERT INTO {table} VALUES (?, ?)",
                        ("C", "2026-08-13"),
                    )
            ok, detail = stage2_retry.tables_complete(
                db_path,
                "2026-08-13",
                {"A", "B", "C"},
            )
            self.assertTrue(ok, detail)

    def test_alerts_once_after_final_failure_and_preserves_exit_code(self):
        run_command = Mock(side_effect=[_Result(3), _Result(4), _Result(9)])
        sleep = Mock()
        notify = Mock(return_value=True)

        rc = stage2_retry.run_pipeline(
            ["/usr/bin/bash", "deploy/run_stage2.sh"],
            max_attempts=3,
            retry_delay=60,
            run_command=run_command,
            sleep=sleep,
            notify=notify,
        )

        self.assertEqual(rc, 9)
        self.assertEqual(run_command.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        notify.assert_called_once()
        message = notify.call_args.args[0]
        self.assertIn("3 次", message)
        self.assertIn("exit=9", message)

    def test_alert_transport_error_does_not_hide_pipeline_exit_code(self):
        notify = Mock(side_effect=OSError("status path full"))
        rc = stage2_retry.run_pipeline(
            ["/usr/bin/bash", "deploy/run_stage2.sh"],
            max_attempts=1,
            retry_delay=0,
            run_command=Mock(return_value=_Result(17)),
            sleep=Mock(),
            notify=notify,
        )
        self.assertEqual(rc, 17)
        notify.assert_called_once()

    def test_default_command_uses_bash_so_script_mode_cannot_cause_203_exec(self):
        self.assertEqual(
            stage2_retry.default_command(BASE),
            ["/usr/bin/bash", str(BASE / "deploy" / "run_stage2.sh")],
        )

    def test_load_env_files_only_fills_missing_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
                "STAGE2_MAX_ATTEMPTS=4\n"
                "STAGE2_RETRY_DELAY_SECONDS='120'\n"
                "TELEGRAM_BOT_TOKEN=must-not-load\n"
                "UNRELATED_SECRET=must-not-load\n"
                "IGNORED_LINE\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"STAGE2_MAX_ATTEMPTS": "existing"}, clear=True):
                stage2_retry.load_env_files([env_file])
                self.assertEqual(os.environ["STAGE2_MAX_ATTEMPTS"], "existing")
                self.assertEqual(os.environ["STAGE2_RETRY_DELAY_SECONDS"], "120")
                self.assertNotIn("TELEGRAM_BOT_TOKEN", os.environ)
                self.assertNotIn("UNRELATED_SECRET", os.environ)

    def test_env_files_do_not_read_mls_intraday_secrets(self):
        self.assertNotIn(
            Path("/opt/mls-intraday/.env"),
            stage2_retry.default_env_files(BASE),
        )

    def test_record_failure_writes_persistent_local_alert(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "stage2-last-failure.json"
            stage2_retry.record_failure("stage2 failed", path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["message"], "stage2 failed")
            self.assertIn("updated_at", payload)

            stage2_retry.record_success(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["message"], "stage2 completed")

    def test_systemd_unit_runs_retry_entrypoint(self):
        unit = (BASE / "deploy" / "mls-screen-stage2.service").read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=/usr/bin/python3 /opt/mls-screen/stage2_retry.py",
            unit,
        )


if __name__ == "__main__":
    unittest.main()
