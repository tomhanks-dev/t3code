"""Exercise cohort safety with simulated hosts and real read-only SQLite reads."""

import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

import host
import manage
import layout
import subprocess


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.config = {"hosts": [{"id": "mac", "transport": "local", "installed_record": str(self.root / "INSTALLED_COMMIT")},
                                 {"id": "ubuntu", "transport": "ssh"}]}
        self.target = "b" * 40
        self.old = "a" * 40
        self.operations = []
        self.busy = {}
        self.fail = None
        self.installed = {h["id"]: self.old for h in self.config["hosts"]}
        self.running = {h["id"]: True for h in self.config["hosts"]}
        for target, value in (("ROOT", self.root), ("STATE", self.root)):
            p = patch.object(manage, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target, value in (("inventory", lambda: self.config), ("host_call", self.host_call)):
            p = patch.object(manage, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.write_pending()
        host.atomic_json(self.root / "idle-observation.json", {"commit": self.target, "inventory_sha256": manage.fingerprint(self.config), "at": time.time() - 120})

    def write_pending(self):
        host.atomic_json(self.root / "pending.json", {"commit": self.target, "inventory_sha256": manage.fingerprint(self.config)})

    def host_call(self, h, operation, *args):
        name = h["id"]
        self.operations.append((name, operation))
        if self.fail == (name, operation):
            raise RuntimeError("simulated host failure")
        if operation == "probe":
            return {"id": name, "busy": self.busy.get(name, {}), "installed": self.installed[name], "target_ready": True, "running": self.running[name]}
        if operation == "stop":
            self.running[name] = False
        elif operation == "activate":
            self.installed[name] = args[0]
        elif operation == "restore-before-start":
            self.installed[name] = args[1]
        elif operation == "start":
            self.running[name] = True
        elif operation == "verify":
            if self.installed[name] != args[0] or not self.running[name]:
                raise AssertionError("Incorrect cohort state")
            return {"installed": self.installed[name], "running": True}
        elif operation == "backup":
            return "/recovery/snapshot.sqlite"

    def deploy(self):
        with contextlib.redirect_stdout(io.StringIO()):
            manage.deploy()

    def test_bootstrap_accepts_only_the_recorded_initial_installations(self):
        self.installed['ubuntu'] = 'official:0.1.7'
        for entry in self.config['hosts']:
            entry['bootstrap'] = {'installed': self.installed[entry['id']]}
        self.write_pending()
        host.atomic_json(self.root / 'idle-observation.json', {'commit': self.target, 'inventory_sha256': manage.fingerprint(self.config), 'at': time.time() - 120})
        self.deploy()
        self.assertEqual(set(self.installed.values()), {self.target})

    def test_matching_markers_do_not_complete_an_unhealthy_deployment(self):
        self.installed = {identity: self.target for identity in self.installed}
        self.fail = ('ubuntu', 'verify')
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertTrue((self.root / 'pending.json').exists())
        self.assertFalse((self.root / 'last-deployment.json').exists())

    def test_busy_host_postpones_everyone(self):
        self.busy["ubuntu"] = {"turns": 1}
        self.deploy()
        self.assertEqual(self.operations, [("mac", "probe"), ("ubuntu", "probe")])
        self.assertFalse((self.root / "deployment.json").exists())
        self.assertFalse((self.root / "idle-observation.json").exists())

    def test_unreachable_host_never_stops_other_host(self):
        self.fail = ("ubuntu", "probe")
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertTrue(all(self.running.values()))
        self.assertEqual(set(self.installed.values()), {self.old})

    def test_first_idle_observation_does_not_cut_over(self):
        (self.root / "idle-observation.json").unlink()
        self.deploy()
        self.assertTrue(all(self.running.values()))
        self.assertTrue((self.root / "pending.json").exists())

    def test_all_hosts_stopped_before_any_activation_and_all_activated_before_start(self):
        self.deploy()
        ops = [operation for _, operation in self.operations]
        self.assertLess(max(i for i, op in enumerate(ops) if op == "stop"), ops.index("activate"))
        self.assertLess(max(i for i, op in enumerate(ops) if op == "activate"), ops.index("start"))
        self.assertEqual(set(self.installed.values()), {self.target})
        self.assertFalse((self.root / "pending.json").exists())
        self.assertEqual(json.loads((self.root / "last-deployment.json").read_text())["phase"], "complete")

    def test_new_queued_work_after_stop_preserves_old_binaries(self):
        self.fail = ("ubuntu", "stopped-check")
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertEqual(set(self.installed.values()), {self.old})
        self.assertTrue(all(self.running.values()))
        self.assertTrue((self.root / "deployment.json").exists())

    def test_partial_activation_restores_cohort_before_start_and_journal_blocks_retry(self):
        self.fail = ("ubuntu", "activate")
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertTrue(all(self.running.values()))
        self.assertEqual(set(self.installed.values()), {self.old})
        self.operations.clear()
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertEqual(self.operations, [])

    def test_start_failure_never_rolls_back_migratable_binaries(self):
        self.fail = ("ubuntu", "start")
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertEqual(set(self.installed.values()), {self.target})
        self.assertEqual(json.loads((self.root / "deployment.json").read_text())["phase"], "starting")
        self.assertTrue((self.root / "pending.json").exists())

    def test_inventory_changed_after_build_cannot_cut_over(self):
        self.config["hosts"].append({"id": "new-host", "transport": "ssh"})
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertEqual(self.operations, [])

    def test_existing_split_cohort_cannot_cut_over(self):
        self.installed["ubuntu"] = "c" * 40
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertTrue(all(self.running.values()))


class DatabaseGuardTest(unittest.TestCase):
    def test_real_database_detects_queued_work_and_imminent_schedules(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            c = sqlite3.connect(database)
            try:
                for table in ["orchestration_v2_projection_runs", "orchestration_v2_projection_provider_turns"]:
                    c.execute(f"CREATE TABLE {table} (completed_at TEXT)")
                c.executescript("""
                    CREATE TABLE orchestration_v2_effect_outbox (status TEXT);
                    CREATE TABLE orchestration_v2_projection_runtime_requests (resolved_at TEXT, status TEXT);
                    CREATE TABLE scheduled_task_webhook_pending (state TEXT);
                    CREATE TABLE scheduled_tasks (enabled INTEGER, last_run_status TEXT, next_run_at TEXT);
                    INSERT INTO orchestration_v2_projection_runs VALUES (NULL);
                    INSERT INTO orchestration_v2_effect_outbox VALUES ('pending');
                    INSERT INTO scheduled_tasks VALUES (1,'succeeded',datetime('now','+1 minute'));
                """)
                c.commit()
                self.assertEqual(host.database_busy(database), {"runs": 1, "effects": 1, "schedules": 1})
                self.assertEqual(host.database_busy(database, include_schedules=False), {"runs": 1, "effects": 1})
            finally:
                c.close()

    def test_unknown_schema_fails_closed_and_reader_never_changes_data(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite"
            with sqlite3.connect(database) as c:
                c.execute("CREATE TABLE sentinel (value TEXT)")
                c.execute("INSERT INTO sentinel VALUES ('preserve')")
            c.close()
            before = database.read_bytes()
            with self.assertRaises(sqlite3.OperationalError):
                host.database_busy(database)
            self.assertEqual(database.read_bytes(), before)


class InfrastructureTest(unittest.TestCase):
    def test_configured_mcp_script_is_allowed_but_other_node_work_still_blocks(self):
        value = {'infrastructure_commands': [{'executable': '/opt/node', 'arguments': ['/tools/mcp.js']}]}
        with patch.object(host.sys, 'platform', 'darwin'), patch.object(host, 'run', return_value='/opt/node /tools/mcp.js'):
            self.assertTrue(host.infrastructure(value, 100, '/opt/node'))
        with patch.object(host.sys, 'platform', 'darwin'), patch.object(host, 'run', return_value='/opt/node /projects/dev-server.js'):
            self.assertFalse(host.infrastructure(value, 100, '/opt/node'))
        self.assertFalse(host.infrastructure(value, 100, '/bin/zsh'))


class BootstrapHostTest(unittest.TestCase):
    def test_initial_mac_activation_and_prestart_restore_preserve_the_dev_app(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commit = 'b' * 40
            app = root / 'Applications/Personal.app'
            staging = root / 'staging'
            target = staging / commit / 'T3 Code (Personal).app'
            target.mkdir(parents=True)
            (target / 'binary').write_text('new')
            host.atomic_json(host.manifest_path(target), host.artifact_manifest(target))
            value = {'platform': 'mac', 'app': str(app), 'home': str(root / 'home'),
                     'stage_root': str(staging), 'bootstrap': {'installed': 'development'}}
            with patch.object(host, 'stopped_check'), patch.object(host, 'target_ready', return_value=True):
                host.activate(value, commit)
                self.assertTrue(app.exists())
                self.assertTrue(host.artifact_valid(app))
                host.restore_before_start(value, commit, 'development')
            self.assertFalse(app.exists())
            self.assertTrue((staging / commit / 'failed.app').exists())
            self.assertEqual(host.installed(value), 'development')


class ArtifactTest(unittest.TestCase):
    def test_seal_detects_modified_added_and_retargeted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 't3').write_text('immutable binary')
            (root / 'current').symlink_to('t3')
            host.atomic_json(root / 'MAINTENANCE_MANIFEST.json', host.artifact_manifest(root))
            self.assertTrue(host.artifact_valid(root))
            (root / 't3').write_text('modified')
            self.assertFalse(host.artifact_valid(root))


class LayoutTest(unittest.TestCase):
    def test_worktree_resolves_the_same_canonical_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            repo, worktree = base / "canonical", base / "feature"
            def git(*args):
                return subprocess.run(["git", *args], check=True, capture_output=True, text=True)
            git("init", "-b", "personal", str(repo))
            git("-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "fixture")
            git("-C", str(repo), "worktree", "add", "--detach", str(worktree))
            self.assertEqual(layout.shared_root(repo), repo.resolve())
            self.assertEqual(layout.shared_root(worktree), repo.resolve())

    def test_missing_inventory_blocks_release_instead_of_assuming_local_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(manage, "LOCAL", Path(directory)):
            with self.assertRaisesRegex(RuntimeError, "Host inventory missing"):
                manage.inventory()


if __name__ == "__main__":
    unittest.main()
