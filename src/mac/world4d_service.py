"""Supervise an external World4D runtime and serve stable cron observations.

Standalone stdlib module: prepare a private, pinned deployment, then run it under
launchd. World data remains in the existing PostgreSQL database.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import re
import runpy
import os
from pathlib import Path
import plistlib
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from .world4d_presence import project_fields
except ImportError:
    from world4d_presence import project_fields


def streaming_handler(base, state):
    """Keep the prototype's event peers bounded and independent of API reads."""

    class Handler(base):
        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def do_GET(self):
            if self.path in {"/mac/world4d/identity", "/mac/world4d/snapshot-on-change"}:
                path = self.path.removeprefix("/mac/world4d")
                try:
                    port = int(os.environ["MAC_WORLD4D_MONITOR_PORT"])
                    with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}{path}", timeout=20
                    ) as response:
                        body = response.read(8001)
                    self.send_response(200)
                except Exception:
                    body = b"World4D monitor unavailable"
                    self.send_response(503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if urllib.parse.urlparse(self.path).path != "/events" or "_subscribers" not in state:
                return super().do_GET()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with state["_sub_lock"]:
                state["_subscribers"].append(self.wfile)
            try:
                while state["_running"]:
                    with state["_sub_lock"]:
                        active = self.wfile in state["_subscribers"]
                    if not active:
                        break  # Simulation writer removed a disconnected peer.
                    time.sleep(0.5)
            finally:
                with state["_sub_lock"]:
                    if self.wfile in state["_subscribers"]:
                        state["_subscribers"].remove(self.wfile)

    return Handler


def run_child(reader: int, script: str) -> None:
    def watch_parent():
        try:
            os.read(reader, 1)
        finally:
            # The owner disappeared: terminate all descendants, including any
            # that ignore TERM. Normal supervisor stops use TERM first.
            os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch_parent, daemon=True).start()
    runtime = runpy.run_path(script, run_name="world4d_service_runtime")
    entry = runtime["main"]
    state = entry.__globals__
    # A long-lived SSE request must not monopolize the prototype's HTTPServer.
    state["HTTPServer"] = ThreadingHTTPServer
    if "Handler" in state:
        state["Handler"] = streaming_handler(state["Handler"], state)
    if "build_snapshot" in state:
        original = state["build_snapshot"]

        def projected_snapshot():
            data = original()
            # The prototype truncates fields to eight; retain health and model
            # timestamps so the real viewer and SSE cannot advertise stale life.
            by_handle = {o.handle: o.fields for o in state["_world"].list(limit=500)}
            for obj in data["objects"]:
                obj["fields"] = project_fields(by_handle.get(obj["handle"], obj["fields"]))
            return data

        state["build_snapshot"] = projected_snapshot
    entry()


def fetch_world(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/world", timeout=3) as response:
        data = json.load(response)
    # The prototype serves {} until its simulation thread has initialized.
    if not isinstance(data.get("tick"), int) or not isinstance(data.get("objects"), list):
        raise ValueError("world has not initialized")
    return data


def fingerprint(name: str, data: dict) -> str:
    objects = data["objects"]
    agents = sum(obj.get("kind") == "agent" for obj in objects)
    danger = sum(bool(obj.get("fields", {}).get("in_danger")) for obj in objects)
    hub = sum(bool(obj.get("fields", {}).get("at_hub")) for obj in objects)
    runtime = sorted(
        (o.get("handle"), project_fields(o.get("fields", {})).get("effective_runtime_status"))
        for o in objects
        if "runtime_valid_until" in o.get("fields", {})
    )
    return (
        f"{name}:agents={agents},danger={danger},hub={hub},paused={int(bool(data.get('paused')))}"
    ) + (f",runtime={json.dumps(runtime, separators=(',', ':'))}" if runtime else "")


def stop_process(proc: subprocess.Popen) -> None:
    # Each daemon owns a separate session, including its psql subprocesses.
    # Kill the group even when the parent already exited: children may survive.
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


class World:
    def __init__(self, settings: dict, config: dict):
        self.settings = settings
        self.config = config
        self.proc = None
        self.started = 0.0
        self.progress = 0.0
        self.last_tick = None
        self.data = None
        self.error = "not started"
        self.restarts = 0
        self.log = None
        self.parent_writer = None

    def start(self) -> None:
        if self.proc is not None:
            stop_process(self.proc)
            self.log.close()
            os.close(self.parent_writer)
            self.proc = self.parent_writer = None
            self.restarts += 1
        name = self.settings["name"]
        self.log = open(Path(self.config["root"]) / f"{name}.log", "ab", buffering=0)
        reader, writer = os.pipe()
        argv = [
            self.config["python"],
            "-u",
            str(Path(__file__).resolve()),
            "child",
            str(reader),
            str(Path(self.config["source"]) / "world4d_daemon.py"),
            "--world",
            name,
            "--port",
            str(self.settings["port"]),
            "--hz",
            str(self.settings.get("hz", 1)),
        ]
        argv.extend(self.settings.get("args", []))
        try:
            self.proc = subprocess.Popen(
                argv,
                env={**os.environ, "MAC_WORLD4D_MONITOR_PORT": str(self.config["monitor_port"])},
                cwd=self.config["source"],
                stdout=self.log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(reader,),
            )
            self.parent_writer = writer
        except BaseException:
            os.close(writer)
            self.log.close()
            raise
        finally:
            os.close(reader)
        self.started = self.progress = time.monotonic()
        self.last_tick = self.data = None
        self.error = "starting"
        print(f"Started {name}, pid={self.proc.pid}", flush=True)

    def poll(self) -> None:
        now = time.monotonic()
        if self.proc.poll() is not None:
            self.error = f"daemon exited {self.proc.returncode}"
            self.data = None
            self.start()
            return
        try:
            data = fetch_world(self.settings["port"])
            if (self.last_tick is not None and data["tick"] != self.last_tick) or data.get(
                "paused"
            ):
                self.progress = now
            self.last_tick = data["tick"]
            # Require an observed advance before declaring startup ready.
            if self.progress == self.started:
                raise ValueError("waiting for simulation progress")
            if now - self.progress > self.config.get("stall_seconds", 90):
                raise ValueError("simulation stopped advancing")
            self.data = data
            self.error = ""
        except Exception as exc:
            self.data = None
            self.error = str(exc)
        if self.error and now - self.progress > self.config.get("stall_seconds", 90):
            print(f"Restarting {self.settings['name']}: {self.error}", flush=True)
            self.start()

    def status(self) -> dict:
        return {
            "name": self.settings["name"],
            "port": self.settings["port"],
            "pid": self.proc.pid if self.proc else None,
            "ready": self.data is not None,
            "error": self.error,
            "tick": self.last_tick,
            "restarts": self.restarts,
        }


def render_snapshot(config: dict) -> str:
    # Import only the renderer; never call its Slack-posting main function.
    script = (
        "import json,sys; from world4d_slack_post import build_post; "
        "worlds=json.loads(sys.argv[1]); "
        "print('🌌 *World4D live snapshot*\\n\\n' + "
        "'\\n\\n'.join(build_post(world=w['name'],port=w['port']) for w in worlds))"
    )
    proc = subprocess.Popen(
        [config["python"], "-c", script, json.dumps(config["worlds"])],
        cwd=config["source"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        output, _errors = proc.communicate(timeout=45)
        if proc.returncode:
            raise RuntimeError("snapshot renderer failed; see runtime source")
        return output
    finally:
        stop_process(proc)


def handler_for(worlds: list[World], config: dict):
    cache = {"fingerprint": None, "body": None}
    cache_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            ready = all(world.data is not None for world in worlds)
            code = 200 if ready else 503
            content_type = "text/plain; charset=utf-8"
            if self.path == "/identity":
                body = json.dumps(
                    {"realm_id": config.get("realm_id"), "world": "fleet", "backend": "prototype"}
                )
                content_type = "application/json"
            elif self.path == "/health":
                body = json.dumps({"ready": ready, "worlds": [w.status() for w in worlds]})
                content_type = "application/json"
            elif self.path == "/monitor":
                # A failed source remains a failure, never a successful offline baseline.
                body = "|".join(
                    fingerprint(w.settings["name"], w.data)
                    if w.data is not None
                    else f"{w.settings['name']}=unavailable"
                    for w in worlds
                )
            elif self.path == "/snapshot-on-change":
                if ready:
                    try:
                        key = "|".join(fingerprint(w.settings["name"], w.data) for w in worlds)
                        with cache_lock:
                            if cache["fingerprint"] != key:
                                snapshot = render_snapshot(config)
                                # Hermes injects at most 8,000 monitor characters.
                                candidate = json.dumps(
                                    {
                                        "realm_id": config.get("realm_id"),
                                        "fingerprint": key,
                                        "snapshot": snapshot,
                                        "summary": "World4D shared simulation\n"
                                        + "\n".join(
                                            f"{w.settings['name']}: {len(w.data['objects'])} objects; "
                                            + ", ".join(
                                                f"{o.get('handle', 'agent')}={project_fields(o.get('fields', {})).get('effective_runtime_status', 'unverified')}"
                                                for o in w.data["objects"]
                                                if o.get("kind") == "agent"
                                            )
                                            for w in worlds
                                        ),
                                    },
                                    ensure_ascii=False,
                                )
                                if len(candidate) > 7800:
                                    raise ValueError("snapshot exceeds cron monitor context limit")
                                cache.update(fingerprint=key, body=candidate)
                            body = cache["body"]
                        content_type = "application/json"
                    except Exception as exc:
                        code, body = 503, str(exc)
                else:
                    body = "World4D is not ready"
            elif self.path == "/snapshot":
                if ready:
                    try:
                        body = render_snapshot(config)
                    except Exception as exc:
                        code, body = 503, str(exc)
                else:
                    body = "World4D is not ready"
            else:
                code, body = 404, "not found"
            encoded = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def load_environment(path: str) -> None:
    env_path = Path(path)
    if env_path.stat().st_mode & 0o077:
        raise ValueError("environment JSON must have mode 0600")
    env = json.loads(env_path.read_text())
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise ValueError("environment must map names to strings")
    os.environ.update(env)
    os.environ["PGCONNECT_TIMEOUT"] = "5"
    os.environ["PGOPTIONS"] = "-c statement_timeout=10000"


def run(config: dict) -> None:
    validate_config(config)
    load_environment(config["environment_file"])
    root = Path(config["root"])
    with (root / "service.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_args: stop.set())
        worlds = [World(settings, config) for settings in config["worlds"]]
        server = ThreadingHTTPServer(
            ("127.0.0.1", config["monitor_port"]), handler_for(worlds, config)
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        try:
            # Refuse to take over an independently running world.
            import socket

            for world in worlds:
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", world.settings["port"])) == 0:
                        raise RuntimeError(f"world port {world.settings['port']} is already in use")
            for world in worlds:
                world.start()
            thread.start()
            while not stop.is_set():
                for world in worlds:
                    world.poll()
                stop.wait(config.get("poll_seconds", 3))
        finally:
            # Idempotent signal handling lets this finish despite repeated TERM.
            for world in worlds:
                if world.proc is not None:
                    stop_process(world.proc)
                    world.log.close()
                    os.close(world.parent_writer)
            if thread.is_alive():
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()


def validate_config(config: dict) -> None:
    ports = [config["monitor_port"]] + [w["port"] for w in config["worlds"]]
    names = [w["name"] for w in config["worlds"]]
    if (
        not names
        or len(names) != len(set(names))
        or not all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in names)
    ):
        raise ValueError("world names must be distinct simple identifiers")
    if len(ports) != len(set(ports)) or not all(
        isinstance(port, int) and 1024 <= port <= 65535 for port in ports
    ):
        raise ValueError("world and monitor ports must be distinct unprivileged ports")
    timings = [config.get("poll_seconds", 3), config.get("stall_seconds", 90)]
    timings.extend(w.get("hz", 1) for w in config["worlds"])
    if not all(math.isfinite(value) and value > 0 for value in timings):
        raise ValueError("timing values must be finite and positive")
    for key in ("python", "root", "source", "environment_file", "home"):
        if not Path(config[key]).is_absolute():
            raise ValueError(f"{key} must be an absolute path")
    if not os.access(config["python"], os.X_OK):
        raise ValueError("python must be executable")


def prepare(config_path: Path) -> Path:
    config = json.loads(config_path.read_text())
    validate_config(config)
    source = Path(config["source"]).resolve()
    root = Path(config["root"]).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    files = sorted(source.glob("*.py")) + sorted((source / "scripts").glob("*.py"))
    required = {"world4d.py", "world4d_daemon.py", "world4d_slack_post.py"}
    if not required <= {p.name for p in files}:
        raise ValueError("source is missing required World4D modules")
    # Snapshot external prototype code privately; do not import its host-specific
    # connection defaults or credentials into this repository.
    hashes = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    runtime = root / "runtimes" / digest
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in files:
        target = runtime / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        if (
            target.exists()
            and hashlib.sha256(target.read_bytes()).hexdigest()
            != hashes[str(path.relative_to(source))]
        ):
            raise ValueError("pinned runtime has changed")
        shutil.copy2(path, target)
    (runtime / "manifest.json").write_text(json.dumps(hashes, indent=2))
    config["source"] = str(runtime)
    config["root"] = str(root)
    installed_config = root / "service.json"
    previous = json.loads(installed_config.read_text()) if installed_config.exists() else {}
    config["realm_id"] = config.get("realm_id") or previous.get("realm_id") or str(uuid.uuid4())
    installed_config.write_text(json.dumps(config, indent=2))
    installed_config.chmod(0o600)
    runner = root / "service.py"
    shutil.copy2(Path(__file__), runner)
    shutil.copy2(Path(__file__).with_name("world4d_presence.py"), root / "world4d_presence.py")
    log = str(root / "supervisor.log")
    plist = {
        "Label": "com.mac.world4d",
        "UserName": config["user"],
        "ProgramArguments": [config["python"], "-u", str(runner), "run", str(installed_config)],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"HOME": config["home"]},
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "ExitTimeOut": 30,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }
    plist_path = root / "com.mac.world4d.plist"
    plist_path.write_bytes(plistlib.dumps(plist))
    print(json.dumps({"plist": str(plist_path), "runtime_digest": digest}))
    return plist_path


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "child":
        reader, script = int(sys.argv[2]), sys.argv[3]
        sys.argv = sys.argv[3:]
        run_child(reader, script)
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    if args.action == "prepare":
        prepare(args.config)
    else:
        run(json.loads(args.config.read_text()))


if __name__ == "__main__":
    main()
