"""Small stdlib-only host operations; also executed through SSH on Linux."""

import base64
import datetime
import json
import hashlib
import os
from pathlib import Path
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".next")
    with temp.open("w") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def database_busy(database, include_schedules=True):
    # A missing table/column is an error, not evidence that an unfamiliar version is idle.
    connection = sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        queries = {
            "runs": "SELECT count(*) FROM orchestration_v2_projection_runs WHERE completed_at IS NULL",
            "turns": "SELECT count(*) FROM orchestration_v2_projection_provider_turns WHERE completed_at IS NULL",
            "effects": "SELECT count(*) FROM orchestration_v2_effect_outbox WHERE status IN ('pending','running')",
            "requests": "SELECT count(*) FROM orchestration_v2_projection_runtime_requests WHERE resolved_at IS NULL AND status = 'pending'",
            "webhooks": "SELECT count(*) FROM scheduled_task_webhook_pending WHERE state IN ('pending','running')",
        }
        if include_schedules:
            queries["schedules"] = "SELECT count(*) FROM scheduled_tasks WHERE enabled = 1 AND (last_run_status = 'running' OR julianday(next_run_at) <= julianday('now', '+2 minutes'))"
        return {key: count for key, sql in queries.items() if (count := connection.execute(sql).fetchone()[0])}
    finally:
        connection.close()


def processes():
    result = {}
    for line in run("ps", "-axo", "pid=,ppid=,comm=").splitlines():
        pid, parent, command = line.strip().split(None, 2)
        if sys.platform.startswith("linux"):
            try:
                command = os.readlink(f"/proc/{pid}/exe")
            except OSError:
                pass
        result[int(pid)] = (int(parent), command)
    return result


def descendants(rows, pid):
    found = {pid}
    while True:
        new = found | {p for p, (parent, _) in rows.items() if parent in found}
        if new == found:
            return found - {pid}
        found = new


def infrastructure(host, pid, command):
    path = Path(command)
    for rule in host.get('infrastructure_commands', []):
        if command != rule['executable']:
            continue
        expected = rule.get('arguments', [])
        if expected:
            args = (Path(f'/proc/{pid}/cmdline').read_bytes().decode().split('\0')
                    if sys.platform.startswith('linux') else shlex.split(run('ps', '-p', str(pid), '-o', 'args=')))
            if args[1:1 + len(expected)] != expected:
                continue
        return True
    if path.name == "t3-resource-monitor":
        return True
    if path.name == "codex":
        if sys.platform.startswith("linux"):
            args = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
        else:
            args = shlex.split(run("ps", "-p", str(pid), "-o", "args="))
        return len(args) > 1 and args[1] == "app-server"
    # Persistent MCP/code-mode transport processes can outlive a completed turn.
    # Arbitrary project Node processes and every shell still block the cutover.
    if command in {"/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node",
                   "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"}:
        return True
    return path.name == "codex-code-mode-host" and str(path).startswith(str(Path.home() / ".codex/packages/"))


def runtime(host):
    path = Path(host["home"]) / "userdata/server-runtime.json"
    rows = processes()
    metadata = json.loads(path.read_text()) if path.exists() else None
    pid = metadata["pid"] if metadata else None
    if host["platform"] == "linux":
        service_pid = int(run("systemctl", "--user", "show", host["service"], "--property=MainPID", "--value"))
        if service_pid == 0:
            bootstrap = host.get('bootstrap', {})
            if pid in rows and Path(f'/proc/{pid}/exe').resolve() == Path(bootstrap.get('executable', '/nonexistent')):
                listener = run('ss', '-H', '-ltnp', 'sport', '=', ':3773')
                if f'pid={pid},' not in listener or str(Path(f'/proc/{pid}/cwd').resolve()) != bootstrap['cwd']:
                    raise RuntimeError('Initial server listener/cwd does not match the pinned bootstrap')
                return pid, pid, rows
            return None, None, rows
        expected = (Path(host["install_root"]) / "current/t3").resolve()
        actual = Path(f"/proc/{service_pid}/exe").resolve()
        if service_pid != pid or actual != expected:
            raise RuntimeError("Service PID/runtime executable mismatch; refusing to operate")
        return service_pid, service_pid, rows
    executable = mac_executable(host)
    matches = {p for p, (_, cmd) in rows.items() if cmd == executable}
    if not matches:
        return None, None, rows
    if pid not in matches:
        raise RuntimeError("Running Mac app has no matching server runtime identity")
    root = rows[pid][0]
    if root not in matches:
        raise RuntimeError("Mac server parent is not the expected app executable")
    return pid, root, rows


def mac_executable(host):
    app = Path(host['app'])
    if app.exists():
        import plistlib
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        return str(app / 'Contents/MacOS' / info['CFBundleExecutable'])
    return host.get('bootstrap', {}).get('executable', str(app / 'Contents/MacOS/T3 Code (Beta)'))


def installed(host):
    path = (Path(host['install_root']) / 'current/SOURCE_COMMIT' if host['platform'] == 'linux'
            else Path(host['app']) / 'Contents/Resources/SOURCE_COMMIT')
    if path.is_file():
        return path.read_text().strip()
    bootstrap = host.get('bootstrap')
    if not bootstrap:
        raise RuntimeError('No verified installation or pinned bootstrap identity')
    return bootstrap['installed']


def target_path(host, commit):
    if host["platform"] == "linux":
        return Path(host["install_root"]) / "releases" / commit
    return Path(host["stage_root"]) / commit / "T3 Code (Personal).app"


def artifact_manifest(root):
    root = Path(root)
    result = {}
    for path in sorted(root.rglob('*')):
        if path.name == 'MAINTENANCE_MANIFEST.json':
            continue
        key = str(path.relative_to(root))
        if path.is_symlink():
            result[key] = {'link': os.readlink(path)}
        elif path.is_file():
            with path.open('rb') as stream:
                result[key] = {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(), 'mode': path.stat().st_mode & 0o777}
    return result


def manifest_path(root):
    root = Path(root)
    return root.with_name(root.name + '.manifest.json') if root.suffix == '.app' else root / 'MAINTENANCE_MANIFEST.json'


def artifact_valid(root):
    path = manifest_path(root)
    return path.is_file() and json.loads(path.read_text()) == artifact_manifest(root)


def target_ready(host, commit):
    target = target_path(host, commit)
    marker = target / ("SOURCE_COMMIT" if host["platform"] == "linux" else "Contents/Resources/SOURCE_COMMIT")
    ready = target / ("READY" if host["platform"] == "linux" else "Contents/Resources/MAINTENANCE_READY")
    return marker.exists() and marker.read_text().strip() == commit and ready.is_file() and artifact_valid(target)


def probe(host, commit=None):
    pid, root, rows = runtime(host)
    busy = database_busy(Path(host["home"]) / "userdata/statev2.sqlite")
    # Any resident agent, shell or project process postpones a cutover, including idle shells.
    child_ids = descendants(rows, pid) if pid else set()
    extra = [p for p in child_ids if not infrastructure(host, p, rows[p][1])]
    if extra:
        busy["application_subprocesses"] = len(extra)
    if pid:
        with urllib.request.urlopen(host["health_url"], timeout=5) as response:
            if response.status != 200:
                raise RuntimeError("Environment health check failed")
            health = json.load(response)
            if health['environmentId'] != host['environment_id']:
                raise RuntimeError('Connected to an unexpected environment')
    return {"id": host["id"], "installed": installed(host), "running": pid is not None,
            "pid": pid, "root_pid": root, "busy": busy,
            "target_ready": target_ready(host, commit) if commit else None}


def stop(host, expected):
    current = probe(host)
    if current["installed"] != expected or current["busy"]:
        raise RuntimeError("Host changed or became busy before shutdown")
    if not current["running"]:
        return
    if host["platform"] == "linux":
        service_pid = int(run('systemctl', '--user', 'show', host['service'], '--property=MainPID', '--value'))
        if service_pid:
            run('systemctl', '--user', 'stop', host['service'])
        else:
            os.kill(current['pid'], signal.SIGTERM)
    else:
        # Only the exact app parent captured and verified by runtime(), never a name-pattern kill.
        os.kill(current["root_pid"], signal.SIGTERM)
    deadline = time.monotonic() + 50
    while True:
        if host["platform"] == "linux":
            remaining = Path(f"/proc/{current['pid']}/exe").exists()
        else:
            rows = processes()
            executable = mac_executable(host)
            remaining = any(rows.get(p, (None, None))[1] == executable for p in (current["pid"], current["root_pid"]))
        if not remaining:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Timed out waiting for host shutdown")
        time.sleep(0.5)


def stopped_check(host):
    if runtime(host)[0] is not None:
        raise RuntimeError("Host must be stopped")
    busy = database_busy(Path(host["home"]) / "userdata/statev2.sqlite", include_schedules=False)
    if busy:
        raise RuntimeError(f"Queued work remains after shutdown: {busy}")


def backup(host, commit):
    stopped_check(host)
    root = Path(host.get("install_root", host["home"])) / "maintenance-backups" / commit
    root.mkdir(parents=True, exist_ok=True)
    destination = root / (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite")
    source = sqlite3.connect((Path(host["home"]) / "userdata/statev2.sqlite").as_uri() + "?mode=ro", uri=True)
    try:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    return str(destination)


def activate(host, commit):
    stopped_check(host)
    if not target_ready(host, commit):
        raise RuntimeError("Target artifact is not ready")
    target = target_path(host, commit)
    if host["platform"] == "linux":
        current = Path(host["install_root"]) / "current"
        next_path = current.with_name("current.maintenance-next")
        next_path.symlink_to(target)
        os.replace(next_path, current)
        marker = Path(host['home']) / 'fork-runner'
        temporary = marker.with_suffix('.next')
        temporary.write_text(str(current / 't3') + '\n')
        os.replace(temporary, marker)
    else:
        app = Path(host["app"])
        app.parent.mkdir(parents=True, exist_ok=True)
        previous = Path(host["stage_root"]) / commit / "previous.app"
        if previous.exists():
            raise RuntimeError("Previous app backup already exists; inspect the prior cutover")
        if app.exists():
            os.rename(app, previous)
            os.rename(manifest_path(app), manifest_path(previous))
        os.rename(target, app)
        os.rename(manifest_path(target), manifest_path(app))
    atomic_json(Path(host['home']) / 'maintenance-activation.json', {'commit': commit, 'at': datetime.datetime.now(datetime.timezone.utc).isoformat()})


def start(host):
    if installed(host) == host.get('bootstrap', {}).get('installed'):
        bootstrap = host['bootstrap']
        env = dict(os.environ, T3CODE_HOME=host['home'], T3CODE_NO_BROWSER='1')
        with (Path(host['home']) / 'maintenance-bootstrap.log').open('ab') as log:
            subprocess.Popen(bootstrap['launch'], cwd=bootstrap['cwd'], env=env,
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        return
    if host["platform"] == "linux":
        run("systemctl", "--user", "start", host["service"])
    else:
        run('open', '-a', host['app'], '--env', 'T3CODE_HOME=' + host['home'], '--env', 'T3CODE_DISABLE_AUTO_UPDATE=true')


def restore_before_start(host, attempted, previous):
    stopped_check(host)
    if host["platform"] == "linux":
        if previous == host.get('bootstrap', {}).get('installed'):
            current = Path(host['install_root']) / 'current'
            if current.is_symlink():
                current.unlink()
            (Path(host['home']) / 'fork-runner').unlink(missing_ok=True)
            return
        target = Path(host["install_root"]) / "releases" / previous
        if (target / "SOURCE_COMMIT").read_text().strip() != previous:
            raise RuntimeError("Original release identity cannot be verified")
        current = Path(host["install_root"]) / "current"
        next_path = current.with_name("current.maintenance-restore")
        next_path.symlink_to(target)
        os.replace(next_path, current)
    else:
        directory = Path(host["stage_root"]) / attempted
        backup = directory / "previous.app"
        app = Path(host["app"])
        if backup.exists():
            marker = backup / "Contents/Resources/SOURCE_COMMIT"
            original = marker.read_text().strip() if marker.exists() else Path(host["installed_record"]).read_text().strip()
            if original != previous:
                raise RuntimeError("Original app identity cannot be verified")
            if app.exists():
                os.rename(app, directory / 'failed.app')
                os.rename(manifest_path(app), manifest_path(directory / 'failed.app'))
            os.rename(backup, app)
            os.rename(manifest_path(backup), manifest_path(app))
        elif previous == host.get('bootstrap', {}).get('installed') and app.exists():
            os.rename(app, directory / 'failed.app')
            os.rename(manifest_path(app), manifest_path(directory / 'failed.app'))
    if installed(host) != previous:
        raise RuntimeError("Original installation was not restored")


def verify(host, commit):
    deadline = time.monotonic() + 50
    last = None
    while time.monotonic() < deadline:
        try:
            value = probe(host)
            if value['running'] and value['installed'] == commit:
                activation = json.loads((Path(host['home']) / 'maintenance-activation.json').read_text())
                metadata = json.loads((Path(host['home']) / 'userdata/server-runtime.json').read_text())
                if activation['commit'] != commit or datetime.datetime.fromisoformat(metadata['startedAt'].replace('Z', '+00:00')) < datetime.datetime.fromisoformat(activation['at']):
                    raise RuntimeError('Runtime predates activation')
                if host['platform'] == 'linux':
                    executable = Path(f"/proc/{value['pid']}/exe").resolve()
                    if executable != Path(host['install_root']) / 'releases' / commit / 't3':
                        raise RuntimeError('Running executable is not the requested immutable release')
                root = (Path(host['install_root']) / 'releases' / commit if host['platform'] == 'linux' else Path(host['app']))
                if not artifact_valid(root):
                    raise RuntimeError('Installed artifact differs from its sealed manifest')
                if host['platform'] == 'mac':
                    run('codesign', '--verify', '--deep', '--strict', host['app'])
                value['verified_source_commit'] = commit
                return value
        except Exception as error:
            last = str(error)
        time.sleep(1)
    raise RuntimeError(f"New environment failed verification: {last}")



def workspace_probe(host, worktree, canonical):
    path = Path(worktree).resolve()
    root = Path(canonical).resolve()
    if path == root or root in path.parents or path in root.parents:
        raise RuntimeError("Refusing protected workspace path")
    database = Path(host["home"]) / "userdata/statev2.sqlite"
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute("SELECT thread_id, payload_json FROM orchestration_v2_projection_threads WHERE deleted_at IS NULL").fetchall()
        owners = [identity for identity, raw in rows
                  if (workspace := json.loads(raw).get("worktreePath")) is not None
                  and Path(workspace).resolve() == path]
        active = 0
        for identity in owners:
            active += connection.execute("SELECT count(*) FROM orchestration_v2_projection_runs WHERE thread_id = ? AND completed_at IS NULL", (identity,)).fetchone()[0]
    finally:
        connection.close()
    exists = path.exists()
    if exists:
        actual = Path(run("git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        if actual != root / ".git":
            raise RuntimeError("Source workspace belongs to another repository")
    # Unknown process cwd visibility must block removal, never certify an idle workspace.
    processes_in_path = True
    if sys.platform.startswith("linux"):
        processes_in_path = False
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            try:
                cwd = (proc / "cwd").resolve(strict=True)
                processes_in_path |= cwd == path or path in cwd.parents
            except FileNotFoundError:
                pass
            except PermissionError:
                processes_in_path = True
    elif sys.platform == "darwin":
        result = subprocess.run(["lsof", "-n", "-a", "-d", "cwd", "-Fn"], text=True, capture_output=True)
        if result.returncode == 0 and not result.stderr:
            processes_in_path = any(Path(line[1:]) == path or path in Path(line[1:]).parents
                                    for line in result.stdout.splitlines() if line.startswith("n/"))
    return dict(exists=exists, head=run("git", "-C", str(path), "rev-parse", "HEAD") if exists else None,
                dirty=bool(run("git", "-C", str(path), "status", "--porcelain", "--untracked-files=all")) if exists else False,
                referenced=bool(owners), active=active, processes=processes_in_path,
                running=runtime(host)[0] is not None)



def cleanup_workspace(host, worktree, canonical, branch, head, release):
    if branch in ("dev", "main") or branch.startswith("-"):
        raise RuntimeError("Protected branch")
    run("git", "check-ref-format", "refs/heads/" + branch)
    root = Path(canonical).resolve()
    path = Path(worktree).resolve()
    def git(*args):
        return run("git", "-C", str(root), *args)
    git("merge-base", "--is-ancestor", head, release)
    remote_ref = "refs/heads/" + branch
    remote = git("ls-remote", "--refs", "origin", remote_ref)
    if remote and remote.split()[0] != head:
        raise RuntimeError("Remote branch advanced")
    for record in git("worktree", "list", "--porcelain").split("\n\n"):
        lines = record.splitlines()
        if "branch " + remote_ref in lines and lines[0] != "worktree " + str(path):
            raise RuntimeError("Branch is checked out in another worktree")
    probe = workspace_probe(host, worktree, canonical)
    if probe["referenced"] or probe["active"]:
        raise RuntimeError("T3 still owns this workspace; retire it through Storage cleanup")
    if probe["exists"]:
        if probe["head"] != head or probe["dirty"]:
            raise RuntimeError("Workspace changed since release")
        if probe["running"] or probe["processes"]:
            raise RuntimeError("Source runtime or workspace processes remain")
        git("worktree", "remove", str(path))
    result = subprocess.run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", remote_ref])
    if result.returncode == 0:
        git("update-ref", "-d", remote_ref, head)
    elif result.returncode != 1:
        raise RuntimeError("Cannot inspect local branch")
    if remote:
        git("push", "--force-with-lease=" + remote_ref + ":" + head, "origin", ":" + remote_ref)
    return {"outcome": "removed", "branch": branch, "worktree": str(path)}


def prepare(host):
    if host['platform'] != 'linux':
        return {'id': host['id'], 'prepared': True}
    unit = Path.home() / '.config/systemd/user' / host['service']
    content = f"""[Unit]
Description=Personal T3 fork (Mac maintenance owns updates)
After=network.target

[Service]
Type=simple
ExecStart={host['install_root']}/current/t3 serve --host 127.0.0.1 --port 3773 --base-dir {host['home']}
WorkingDirectory={Path.home()}
Environment=PATH={Path.home()}/.local/bin:{Path.home()}/.cargo/bin:/usr/local/bin:/usr/bin:/bin
Environment=T3CODE_NO_BROWSER=1
Environment=TZ=Europe/Zurich
Restart=on-failure
RestartSec=5
TimeoutStopSec=60

[Install]
WantedBy=default.target
"""
    if unit.exists() and unit.read_text() != content:
        raise RuntimeError('Existing service differs; preserve and reconcile it')
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    run('systemctl', '--user', 'daemon-reload')
    run('systemctl', '--user', 'enable', host['service'])
    run('loginctl', 'enable-linger', str(os.getuid()))
    if run('loginctl', 'show-user', str(os.getuid()), '--property=Linger', '--value') != 'yes':
        raise RuntimeError('User service cannot survive logout; lingering is required')
    return {'id': host['id'], 'prepared': True, 'service': str(unit)}


def main():
    if sys.argv[1] == 'seal':
        root = Path(sys.argv[2])
        atomic_json(manifest_path(root), artifact_manifest(root))
        return
    operation, encoded, *args = sys.argv[1:]
    host = json.loads(base64.urlsafe_b64decode(encoded))
    functions = {"prepare": prepare, "probe": probe, "stop": stop, "backup": backup, "activate": activate,
                 "start": start, "verify": verify, "stopped-check": stopped_check,
                 "restore-before-start": restore_before_start, "workspace-probe": workspace_probe, "workspace-cleanup": cleanup_workspace}
    result = functions[operation](host, *args)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
