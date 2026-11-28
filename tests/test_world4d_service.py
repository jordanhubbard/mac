"""Real process controls for startup, progress loss, restart and bounded shutdown."""

import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest

from mac import world4d_service as service
from tests.world4d_nonce_fixture import keeper_cleanup_proof


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(port, path="/health"):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
        return response.read().decode()


def eventually(fn, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "external source"
    source.mkdir()
    (source / "world4d.py").write_text("")
    (source / "world4d_slack_post.py").write_text(
        "def build_post(world, port):\n    return f'{world} snapshot on {port}'\n"
        "def main():\n    raise AssertionError('must never post directly')\n"
    )
    (source / "world4d_daemon.py").write_text("""
import argparse, json, time, os, uuid
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, HTTPServer
p=argparse.ArgumentParser()
p.add_argument('--world'); p.add_argument('--port',type=int); p.add_argument('--hz')
a=p.parse_args(); start=time.monotonic(); frozen=None; danger=False
fixture_instance=uuid.uuid4().hex
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        global frozen, danger
        route=urlparse(self.path)
        if route.path=='/fixture-proof':
            challenge=parse_qs(route.query).get('challenge',[''])[0]
            if len(challenge)!=32 or any(c not in '0123456789abcdef' for c in challenge):
                self.send_error(400); return
            body=json.dumps({'instance':fixture_instance,'pid':os.getpid(),
                             'world':a.world,'challenge':challenge}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path=='/hold':
            self.send_response(200); self.end_headers(); time.sleep(3); return
        tick=int((time.monotonic()-start)*10) if frozen is None else frozen
        if self.path=='/freeze': frozen=tick
        if self.path=='/danger': danger=not danger
        body=json.dumps({'tick':tick,'paused':False,'objects':[{'kind':'agent','fields':{'in_danger':danger}}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
def main():
    HTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
if __name__=='__main__': main()
""")
    env = tmp_path / "environment.json"
    env.write_text(json.dumps({"PATH": os.environ["PATH"]}))
    env.chmod(0o600)
    root = tmp_path / "service"
    root.mkdir()
    ports = set()
    while len(ports) < 3:
        ports.add(free_port())
    monitor, first, second = sorted(ports)
    return {
        "root": str(root),
        "source": str(source),
        "python": sys.executable,
        "environment_file": str(env),
        "monitor_port": monitor,
        "worlds": [{"name": "test", "port": first}, {"name": "fleet", "port": second}],
        "stall_seconds": 0.7,
        "poll_seconds": 0.05,
        "user": "example",
        "home": str(tmp_path),
        # The process fixture intentionally has no database or physics engine.
        "storage_guard": False,
    }


def launch(config):
    path = Path(config["root"]) / "config.json"
    path.write_text(json.dumps(config))
    return subprocess.Popen([sys.executable, service.__file__, "run", str(path)])


def test_runtime_restarts_failed_and_stalled_children_and_stops_cleanly(config):
    proc = launch(config)
    known_pids = set()
    port = config["monitor_port"]
    try:
        status = eventually(lambda: json.loads(get(port)))
        assert status["ready"]
        known_pids.update(w["pid"] for w in status["worlds"])
        with urllib.request.urlopen(
            f"http://127.0.0.1:{config['worlds'][0]['port']}/hold", timeout=2
        ):
            assert json.loads(get(config["worlds"][0]["port"], "/world"))["objects"]
        baseline = get(port, "/monitor")
        time.sleep(0.2)
        assert get(port, "/monitor") == baseline
        snapshot = get(port, "/snapshot")
        assert "test snapshot" in snapshot and "fleet snapshot" in snapshot
        assert "\n\n" in snapshot
        cached = get(port, "/snapshot-on-change")
        assert json.loads(cached)["snapshot"] == snapshot
        time.sleep(0.2)
        assert get(port, "/snapshot-on-change") == cached
        # A remote reporter reads through the existing world listener. Forwarding
        # back through the supervisor must not starve readiness or its renderer.
        proxy_port = config["worlds"][1]["port"]
        assert get(proxy_port, "/mac/world4d/snapshot-on-change") == cached
        assert json.loads(get(proxy_port, "/mac/world4d/identity")) == {
            "realm_id": None,
            "world": "fleet",
            "backend": "prototype",
        }
        get(config["worlds"][0]["port"], "/danger")
        eventually(lambda: "danger=1" in get(port, "/monitor"))
        assert get(port, "/snapshot-on-change") != cached
        first_pid = status["worlds"][0]["pid"]
        os.kill(first_pid, signal.SIGKILL)

        def restarted():
            data = json.loads(get(port))
            return data if data["ready"] and data["worlds"][0]["pid"] != first_pid else None

        status = eventually(restarted)
        assert status["worlds"][0]["restarts"] == 1
        known_pids.add(status["worlds"][0]["pid"])
        second_pid = status["worlds"][1]["pid"]
        get(config["worlds"][1]["port"], "/freeze")
        status = eventually(
            lambda: (
                (d if d["ready"] and d["worlds"][1]["pid"] != second_pid else None)
                if (d := json.loads(get(port)))
                else None
            )
        )
        known_pids.add(status["worlds"][1]["pid"])
        assert status["worlds"][1]["restarts"] == 1
    finally:
        proc.terminate()
        assert proc.wait(timeout=10) == 0
    for pid in known_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_unready_worlds_fail_monitor_and_snapshot(config):
    world = service.World(config["worlds"][0], config)
    server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.handler_for([world], config))
    thread = service.threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        for path in ("/health", "/monitor", "/snapshot", "/snapshot-on-change"):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(server.server_port, path)
            assert error.value.code == 503
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_prepare_pins_runtime_and_generates_boot_service(config):
    path = Path(config["root"]) / "input.json"
    path.write_text(json.dumps(config))
    plist_path = service.prepare(path)
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["RunAtLoad"] and plist["KeepAlive"]
    assert plist["UserName"] == "example"
    installed = json.loads((Path(config["root"]) / "service.json").read_text())
    assert installed["realm_id"]
    assert (Path(config["root"]) / "world4d_presence.py").is_file()
    service.prepare(path)
    assert (
        json.loads((Path(config["root"]) / "service.json").read_text())["realm_id"]
        == installed["realm_id"]
    )
    pinned = Path(installed["source"])
    assert (pinned / "manifest.json").exists()
    (Path(config["source"]) / "world4d.py").write_text("# later source edit")
    assert (pinned / "world4d.py").read_text() == ""
    (pinned / "world4d.py").write_text("tampered")
    # Use original input bytes so this addresses the same pinned generation.
    (Path(config["source"]) / "world4d.py").write_text("")
    with pytest.raises(ValueError, match="pinned runtime has changed"):
        service.prepare(path)


def test_environment_rejects_world_readable_secrets(config):
    path = Path(config["environment_file"])
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        service.load_environment(str(path))


def test_install_refuses_ambiguous_ports_and_unsafe_log_names(config):
    config["worlds"][1]["port"] = config["worlds"][0]["port"]
    with pytest.raises(ValueError, match="ports must be distinct"):
        service.validate_config(config)
    config["worlds"][1]["port"] = free_port()
    config["worlds"][0]["name"] = "../other-service"
    with pytest.raises(ValueError, match="simple identifiers"):
        service.validate_config(config)


@pytest.mark.process_e2e
def test_supervisor_crash_releases_world_ports_for_its_replacement(config):
    artifact = Path(config["root"]) / "crash-release-evidence"
    artifact.mkdir(mode=0o700)
    evidence = {"schema": "mac.world4d.original_crash_fixture.v1", "phases": [], "snapshots": []}
    started = time.monotonic()

    def record():
        path = artifact / "observations.json"
        path.write_text(json.dumps(evidence, sort_keys=True) + "\n")
        path.chmod(0o600)

    def snapshot(phase):
        # These are THIS original service's keeper files, never the separate
        # TREE fixtures. Preserve exact raw fixed-field bytes even on failure.
        files = []
        for source in sorted(Path(config["root"]).glob("guardian-*.jsonl")):
            raw = source.read_bytes()
            target = artifact / (phase + "-" + source.name)
            target.write_bytes(raw)
            target.chmod(0o600)
            files.append(
                {
                    "file": source.name,
                    "snapshot": target.name,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                }
            )
        evidence["snapshots"].append(
            {"phase": phase, "elapsed": time.monotonic() - started, "files": files}
        )
        record()

    def application(port, deadline):
        challenge = uuid.uuid4().hex
        result = {"port": port, "challenge": challenge, "valid": False, "outcome": "no_reply"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        try:
            # Fresh challenge is echoed by the fixture, not supplied identity;
            # stale HTTP/proxy data cannot stand in for a live payload instance.
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/fixture-proof?challenge={challenge}",
                timeout=min(0.1, remaining),
            ) as response:
                body = response.read(513)
            proof = json.loads(body)
            if (
                len(body) <= 512
                and type(proof) is dict
                and proof.get("challenge") == challenge
                and type(proof.get("pid")) is int
                and proof["pid"] > 0
                and isinstance(proof.get("instance"), str)
                and len(proof["instance"]) == 32
                and all(c in "0123456789abcdef" for c in proof["instance"])
            ):
                result.update(
                    valid=True,
                    outcome="matched",
                    instance=proof["instance"],
                    pid=proof["pid"],
                    world=proof.get("world"),
                )
            else:
                result["outcome"] = "invalid_reply"
        except (OSError, ValueError, urllib.error.URLError) as exc:
            result["outcome"] = "no_reply"
            result["errno"] = (
                exc.errno if isinstance(exc, OSError) and type(exc.errno) is int else None
            )
        return result

    def ready_instance(deadline, phase):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        with urllib.request.urlopen(
            f"http://127.0.0.1:{config['monitor_port']}/health", timeout=min(2, remaining)
        ) as response:
            status = json.load(response)
        proofs = {world["port"]: application(world["port"], deadline) for world in status["worlds"]}
        evidence["phases"].append(
            {"phase": phase, "elapsed": time.monotonic() - started, "proofs": proofs}
        )
        record()
        expected = {(w["name"], w["port"]) for w in config["worlds"]}
        observed = {(w["name"], w["port"]) for w in status["worlds"]}
        if (
            status["ready"]
            and observed == expected
            and len(status["worlds"]) == len(expected)
            and all(
                proofs[w["port"]].get("valid")
                and proofs[w["port"]]["pid"] == w["pid"]
                and proofs[w["port"]].get("world") == w["name"]
                for w in status["worlds"]
            )
            and time.monotonic() <= deadline
        ):
            return status, proofs
        return None

    pinned_keepers = []
    keeper_observation = 0

    def keeper_drain_proof():
        nonlocal keeper_observation
        if not sys.platform.startswith("linux"):
            return {"required": False, "platform": "portable", "proven": None}
        keeper_observation += 1
        rows = []
        for source, identity, prefix in pinned_keepers:
            row = {"file": source.name, "proven": False}
            try:
                info = source.stat()
                raw = source.read_bytes()
                target = artifact / (f"keeper-proof-{keeper_observation:03d}-" + source.name)
                target.write_bytes(raw)
                target.chmod(0o600)
                row.update(
                    snapshot=target.name, sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw)
                )
                row["same_pinned_file"] = (info.st_dev, info.st_ino) == identity and raw.startswith(
                    prefix
                )
                row.update(
                    keeper_cleanup_proof(
                        raw,
                        pinned_prefix=prefix,
                        same_file=(info.st_dev, info.st_ino) == identity,
                        posix=os.name == "posix",
                        supervisor_returncode=proc.returncode,
                        now=time.monotonic(),
                        release_deadline=release_deadline,
                    )
                )
            except (OSError, ValueError) as exc:
                # A partial append or not-yet-drained keeper is retried ONLY
                # inside the already-running original release12s window.
                row["errno"] = exc.errno if isinstance(exc, OSError) else None
            rows.append(row)
        return {
            "required": True,
            "files": rows,
            "deadline": min((row["deadline"] for row in rows if row.get("proven")), default=None),
            "proven": len(rows) == len(config["worlds"]) == 2
            and all(row["proven"] for row in rows),
        }

    proc = launch(config)
    replacement = None
    try:
        helper = Path(service.__file__).with_name("world4d_owned_child.py")
        if helper.is_file():
            raw = helper.read_bytes()
            (artifact / "actual-service-helper.py").write_bytes(raw)
            (artifact / "actual-service-helper.py").chmod(0o400)
            evidence["service_helper_sha256"] = hashlib.sha256(raw).hexdigest()
        # No autonomous fixture exit is introduced to mask crash cleanup.
        # Each existing eventually window retains its original12s budget.
        startup_deadline = time.monotonic() + 12
        status, old_proofs = eventually(lambda: ready_instance(startup_deadline, "before-crash"))
        old_pids = {world["pid"] for world in status["worlds"]}
        evidence["old_pids"] = sorted(old_pids)
        snapshot("before-crash")
        if sys.platform.startswith("linux"):
            # No renderer/snapshot child is launched by this fixture. Pin the
            # TWO original world keepers before death, never replacement files.
            for source in sorted(Path(config["root"]).glob("guardian-*.jsonl")):
                info = source.stat()
                pinned_keepers.append((source, (info.st_dev, info.st_ino), source.read_bytes()))
            evidence["pinned_precrash_keepers"] = [
                {
                    "file": source.name,
                    "device": identity[0],
                    "inode": identity[1],
                    "prefix_sha256": hashlib.sha256(raw).hexdigest(),
                    "prefix_bytes": len(raw),
                }
                for source, identity, raw in pinned_keepers
            ]
            record()
            assert len(pinned_keepers) == len(config["worlds"]) == 2
        proc.kill()
        proc.wait(timeout=5)
        release_deadline = time.monotonic() + 12
        evidence["supervisor_returncode"] = proc.returncode
        record()
        if os.name == "posix":
            assert proc.returncode == -signal.SIGKILL

        def ports_released():
            proofs = {
                world["port"]: application(world["port"], release_deadline)
                for world in config["worlds"]
            }
            attempts = []
            reservations = []
            try:
                for world in config["worlds"]:
                    port = world["port"]
                    proof = proofs[port]
                    row = {
                        "port": port,
                        "application": proof,
                        "tcp_result": None,
                        "bind_listen": False,
                    }
                    remaining = release_deadline - time.monotonic()
                    if remaining > 0:
                        with socket.socket() as diagnostic:
                            diagnostic.settimeout(min(0.05, remaining))
                            try:
                                row["tcp_result"] = diagnostic.connect_ex(("127.0.0.1", port))
                            except OSError as exc:
                                row["tcp_errno"] = exc.errno
                    # Actual binding/listening at every EXACT original port is
                    # mandatory; no SO_REUSEPORT, no replacement masking a leak.
                    reserved = socket.socket()
                    reservations.append(reserved)
                    reserved.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        reserved.bind(("127.0.0.1", port))
                        reserved.listen(1)
                        row["bind_listen"] = True
                    except OSError as exc:
                        row["bind_errno"] = exc.errno
                    row["old_identity_absent"] = not (
                        proof.get("valid") and proof.get("instance") == old_proofs[port]["instance"]
                    )
                    attempts.append(row)
                keeper_proof = keeper_drain_proof()
                evidence["phases"].append(
                    {
                        "phase": "release",
                        "keeper_proof": keeper_proof,
                        "elapsed": time.monotonic() - started,
                        "attempts": attempts,
                    }
                )
                record()
                success = (
                    time.monotonic() <= release_deadline
                    and (
                        not keeper_proof["required"]
                        or keeper_proof["proven"]
                        and time.monotonic() <= keeper_proof["deadline"]
                    )
                    and all(row["bind_listen"] and row["old_identity_absent"] for row in attempts)
                )
                if success:
                    evidence["chosen_release_proof"] = evidence["phases"][-1]
                    record()
                    if time.monotonic() <= release_deadline and (
                        not keeper_proof["required"] or time.monotonic() <= keeper_proof["deadline"]
                    ):
                        return True
                    evidence.pop("chosen_release_proof")
                    record()
                return False
            finally:
                for reserved in reservations:
                    reserved.close()

        eventually(ports_released)
        snapshot("released")
        replacement = launch(config)
        replacement_deadline = time.monotonic() + 12
        status, new_proofs = eventually(lambda: ready_instance(replacement_deadline, "replacement"))
        assert status["ready"]
        assert old_pids.isdisjoint(world["pid"] for world in status["worlds"])
        assert all(
            new_proofs[world["port"]]["instance"] != old_proofs[world["port"]]["instance"]
            for world in config["worlds"]
        )
    finally:
        # Evidence I/O failures must still run the ORIGINAL cleanup; they are
        # errors, never permission to leave a fixture supervisor alive.
        try:
            snapshot("before-final-cleanup")
        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=10)
                if replacement is not None:
                    replacement.terminate()
                    assert replacement.wait(timeout=10) == 0
            finally:
                snapshot("after-final-cleanup")


@pytest.fixture
def storage_database(monkeypatch):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from mac.test_support import test_dsn
    from tests.pg_worker_databases import WorkerDatabases

    databases = WorkerDatabases(test_dsn())
    dsn = databases.create()
    params = conninfo_to_dict(dsn)
    for key, value in {
        "W4D_HOST": params.get("host", "/tmp"),
        "W4D_PORT": params.get("port", "5432"),
        "W4D_DB": params["dbname"],
        "W4D_USER": params.get("user", os.environ.get("USER", "postgres")),
        "W4D_PASS": params.get("password", ""),
    }.items():
        monkeypatch.setenv(key, value)
    # Deliberately conflicting memory-bridge defaults must never select its DB.
    monkeypatch.setenv("PGDATABASE", "wrong_memory_store")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("""
        CREATE EXTENSION cube;
        CREATE SCHEMA world4d;
        CREATE TABLE world4d.objects (
            id integer PRIMARY KEY, px float8, py float8, pz float8, pw float8,
            pos4d cube, sim_tick bigint DEFAULT 0);
        CREATE FUNCTION world4d.sync_pos4d() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.pos4d := cube(ARRAY[NEW.px,NEW.py,NEW.pz,NEW.pw]); RETURN NEW; END;
        $$;
        CREATE TRIGGER trg_sync_pos4d BEFORE INSERT OR UPDATE ON world4d.objects
        FOR EACH ROW EXECUTE FUNCTION world4d.sync_pos4d();
        CREATE INDEX idx_objects_pos ON world4d.objects (px,py,pz,pw);
        CREATE INDEX idx_objects_pos4d ON world4d.objects USING gist(pos4d);
        INSERT INTO world4d.objects(id,px,py,pz,pw)
        SELECT n,n,0,0,0 FROM generate_series(1,183) n;
        ANALYZE world4d.objects;
    """)
    try:
        yield conn
    finally:
        conn.close()
        databases.close()


def test_storage_policy_preserves_physics_and_repairs_index_churn(storage_database):
    conn = storage_database
    guard = service.StorageGuard()
    guard.poll(force=True)
    assert guard.ready, guard.error
    assert (
        "autovacuum_vacuum_cost_delay=0"
        in conn.execute(
            "SELECT reloptions FROM pg_class WHERE oid='world4d.objects'::regclass"
        ).fetchone()[0]
    )
    # Repeated actual position updates, each committed independently as in the
    # psql-based runtime, then ordinary vacuum and the real atomic rebuild.
    for _ in range(100):
        conn.execute("UPDATE world4d.objects SET px=px+0.5, sim_tick=sim_tick+1")
    conn.execute("VACUUM ANALYZE world4d.objects")
    before = conn.execute(
        "SELECT id,px,sim_tick,pos4d::text FROM world4d.objects ORDER BY id"
    ).fetchall()
    assert len(before) == 183 and before[0][1:3] == (51, 100)
    # Force the threshold path on a small real index without allocating 64 MiB.
    original = guard.measure
    old_files = conn.execute(
        "SELECT indexrelid,pg_relation_filenode(indexrelid) FROM pg_index WHERE indrelid='world4d.objects'::regclass ORDER BY indexrelid"
    ).fetchall()
    calls = 0

    def over_budget_once():
        nonlocal calls
        rows = original()
        calls += 1
        if calls == 1:
            for row in rows:
                row["budget"] = 0
        return rows

    guard.measure = over_budget_once
    guard.poll(force=True)
    assert guard.ready, guard.error
    new_files = conn.execute(
        "SELECT indexrelid,pg_relation_filenode(indexrelid) FROM pg_index WHERE indrelid='world4d.objects'::regclass ORDER BY indexrelid"
    ).fetchall()
    assert sum(a != b for a, b in zip(old_files, new_files)) == 2
    assert (
        conn.execute(
            "SELECT id,px,sim_tick,pos4d::text FROM world4d.objects ORDER BY id"
        ).fetchall()
        == before
    )
    assert all(i["valid"] and i["bytes"] < 1024 * 1024 for i in guard.indexes)
    # Unchanged coordinates retain their spatial value; moving in W still changes
    # four-dimensional proximity correctly, independent of the rebuild.
    conn.execute("UPDATE world4d.objects SET sim_tick=sim_tick+1 WHERE id=1")
    assert (
        conn.execute("SELECT pos4d::text FROM world4d.objects WHERE id=1").fetchone()[0]
        == before[0][3]
    )
    conn.execute("UPDATE world4d.objects SET pw=3 WHERE id=1")
    assert (
        conn.execute(
            "SELECT cube_distance(pos4d,cube(ARRAY[51.,0.,0.,0.])) FROM world4d.objects WHERE id=1"
        ).fetchone()[0]
        == 3
    )


def test_storage_failure_is_not_ready_and_stops_our_writers(config):
    # Missing required world connection settings fail closed. The fixture's
    # HTTP-only children must never start and advertise healthy progress.
    config["storage_guard"] = True
    proc = launch(config)
    try:

        def unready():
            try:
                get(config["monitor_port"])
            except urllib.error.HTTPError as error:
                return json.load(error)

        status = eventually(unready)
        assert not status["ready"] and not status["storage"]["ready"]
        assert all(w["pid"] is None for w in status["worlds"])
    finally:
        proc.terminate()
        assert proc.wait(timeout=10) == 0
