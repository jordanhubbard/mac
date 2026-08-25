from __future__ import annotations

from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL = ROOT / "deploy" / "fleet-node-install.sh"


def _lifecycle_functions() -> str:
    source = NODE_INSTALL.read_text(encoding="utf-8")
    body = source.split("supervisord_program_state() {", 1)[1].split(
        "\n\nstop_existing_services_for_deploy() {", 1
    )[0]
    return "supervisord_program_state() {" + body


def _run_lifecycle(
    tmp_path: Path,
    command: str,
    records: list[tuple[int, str]],
) -> subprocess.CompletedProcess[str]:
    statuses = " ".join(shlex.quote(str(rc)) for rc, _output in records)
    outputs = " ".join(shlex.quote(output) for _rc, output in records)
    events = tmp_path / "events"
    counter = tmp_path / "counter"
    script = tmp_path / "case.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"FAKE_STATUS_RCS=({statuses})\n"
        f"FAKE_STATUS_OUTPUTS=({outputs})\n"
        f"FAKE_STATUS_COUNTER={shlex.quote(str(counter))}\n"
        "printf '0\\n' > \"$FAKE_STATUS_COUNTER\"\n"
        f"FAKE_EVENTS={shlex.quote(str(events))}\n"
        "log() { printf '%s\\n' \"$*\" >&2; }\n"
        "run_supervisorctl() {\n"
        '  local operation="$1"\n'
        "  shift\n"
        '  case "$operation" in\n'
        "    status)\n"
        "      local index\n"
        '      index="$(<"$FAKE_STATUS_COUNTER")"\n'
        '      printf \'%s\\n\' "$(( index + 1 ))" > "$FAKE_STATUS_COUNTER"\n'
        "      printf '%s\\n' \"${FAKE_STATUS_OUTPUTS[$index]}\"\n"
        '      return "${FAKE_STATUS_RCS[$index]}"\n'
        "      ;;\n"
        "    start|stop)\n"
        '      printf \'%s %s\\n\' "$operation" "$1" >> "$FAKE_EVENTS"\n'
        "      ;;\n"
        "    *) return 64 ;;\n"
        "  esac\n"
        "}\n" + _lifecycle_functions() + "\n" + command + "\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ["/bin/bash", str(script)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def test_exact_state_accepts_one_matching_running_program(tmp_path: Path) -> None:
    result = _run_lifecycle(
        tmp_path,
        "supervisord_program_state mac-agent",
        [(0, "mac-agent RUNNING pid 412, uptime 0:00:30")],
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "running\n"


def test_exact_state_rejects_wrong_identity_or_multiple_records(tmp_path: Path) -> None:
    wrong = _run_lifecycle(
        tmp_path,
        "supervisord_program_state mac-agent",
        [(0, "other-agent RUNNING pid 412, uptime 0:00:30")],
    )
    assert wrong.returncode != 0
    assert "wrong program identity" in wrong.stderr

    multiple = _run_lifecycle(
        tmp_path,
        "supervisord_program_state mac-agent",
        [(0, "mac-agent RUNNING pid 412, uptime 0:00:30\nother STOPPED")],
    )
    assert multiple.returncode != 0
    assert "multiple records" in multiple.stderr


def test_stop_and_start_use_one_exact_manager_state_sequence(tmp_path: Path) -> None:
    stopped = _run_lifecycle(
        tmp_path,
        "stop_supervisord_program_if_present mac-agent",
        [
            (0, "mac-agent RUNNING pid 412, uptime 0:00:30"),
            (3, "mac-agent STOPPED Not started"),
        ],
    )
    assert stopped.returncode == 0, stopped.stderr
    assert (tmp_path / "events").read_text(encoding="utf-8") == "stop mac-agent\n"

    (tmp_path / "events").unlink()
    started = _run_lifecycle(
        tmp_path,
        "start_supervisord_program mac-agent",
        [
            (3, "mac-agent STOPPED Not started"),
            (0, "mac-agent RUNNING pid 413, uptime 0:00:01"),
        ],
    )
    assert started.returncode == 0, started.stderr
    assert (tmp_path / "events").read_text(encoding="utf-8") == "start mac-agent\n"


def test_start_refuses_an_absent_program(tmp_path: Path) -> None:
    result = _run_lifecycle(
        tmp_path,
        "start_supervisord_program mac-agent",
        [(3, "mac-agent: ERROR (no such process)")],
    )
    assert result.returncode != 0
    assert "cannot start absent" in result.stderr
    assert not (tmp_path / "events").exists()
