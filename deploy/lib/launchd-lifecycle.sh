#!/usr/bin/env bash

# One lifecycle contract for standalone launchd service installers.
#
# State inspection is tri-state and fail-closed.  Time bounds use Python's
# monotonic clock because Bash 3.2 implements SECONDS from time(3), which can
# move when the wall clock is adjusted.  Every child runs in a fresh process
# group and is terminated, then killed, when its absolute deadline expires.
# Public job helpers preserve their existing arguments and accept an optional
# final execution mode: "user" (the default) or "system" (sudo -n). A
# transaction records the mode passed to mac_launchd_transaction_begin so EXIT
# compensation uses the identical control and artifact privilege boundary.

mac_launchd_error() {
  printf '%s\n' "${MAC_LAUNCHD_LOG_PREFIX:-[launchd]} ERROR: $*" >&2
}

mac_launchd_python_bin() {
  local candidate="${MAC_LAUNCHD_PYTHON_BIN:-}"
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  candidate="$(command -v python3 2>/dev/null || true)"
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  mac_launchd_error "Python 3 is required for monotonic bounded lifecycle commands"
  return 1
}

mac_launchd_validate_mode() {
  case "${1:-user}" in
    user|system) return 0 ;;
    *)
      mac_launchd_error "unsupported launchd execution mode: ${1:-}"
      return 2
      ;;
  esac
}

# Execute launchctl from a direct argv.  System-domain operations deliberately
# use sudo's non-interactive mode; a shell command prefix would make argument
# boundaries ambiguous and is never evaluated here.
mac_launchd_run_control_bounded() {
  local mode="$1" command_timeout="$2"
  shift 2
  mac_launchd_validate_mode "$mode" || return $?
  case "$mode" in
    user)
      mac_run_bounded "$command_timeout" launchctl "$@"
      ;;
    system)
      mac_run_bounded "$command_timeout" sudo -n launchctl "$@"
      ;;
  esac
}

# Run a Python program supplied as one argv value under the same process-group
# bound.  This avoids feeding a second program through stdin (which belongs to
# the supervisor) and keeps privileged artifact operations free of shell eval.
mac_launchd_run_python_bounded() {
  local mode="$1" command_timeout="$2" program="$3" python_bin=""
  shift 3
  mac_launchd_validate_mode "$mode" || return $?
  python_bin="$(mac_launchd_python_bin)" || return $?
  case "$mode" in
    user)
      mac_run_bounded "$command_timeout" "$python_bin" -c "$program" "$@"
      ;;
    system)
      mac_run_bounded "$command_timeout" sudo -n "$python_bin" -c "$program" "$@"
      ;;
  esac
}

# stdout and stderr retain their normal separation so bounded commands remain
# safe in pipelines and command substitutions. Output is drained without
# blocking into capped temporary files: a descendant which escapes the original
# process group and retains a pipe cannot hold the lifecycle supervisor open.
# Exit 124 is the timeout sentinel; exit 125 means the output cap was exceeded.
mac_run_bounded() {
  local command_timeout="$1" python_bin="" output_limit=""
  shift
  [ "$#" -gt 0 ] || {
    mac_launchd_error "bounded command requires an argv"
    return 2
  }
  python_bin="$(mac_launchd_python_bin)" || return $?
  output_limit="${MAC_LAUNCHD_MAX_OUTPUT_BYTES:-1048576}"
  "$python_bin" - "$command_timeout" "$output_limit" "$@" <<'PY'
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

try:
    timeout = float(sys.argv[1])
except (TypeError, ValueError):
    raise SystemExit("invalid bounded-command timeout")
if not math.isfinite(timeout) or timeout <= 0:
    raise SystemExit("bounded-command timeout must be finite and positive")
try:
    output_limit = int(sys.argv[2])
except (TypeError, ValueError):
    raise SystemExit("invalid bounded-command output limit")
if output_limit <= 0 or output_limit > 16 * 1024 * 1024:
    raise SystemExit("bounded-command output limit is out of range")
argv = sys.argv[3:]


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except OSError:
        pass


def run_capped():
    stdout_file = tempfile.TemporaryFile()
    stderr_file = tempfile.TemporaryFile()
    try:
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return None, b"", b"", False, False, exc
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        streams = {
            process.stdout.fileno(): [process.stdout, stdout_file, 0],
            process.stderr.fileno(): [process.stderr, stderr_file, 0],
        }
        try:
            for descriptor, (pipe, _capture, _size) in streams.items():
                os.set_blocking(descriptor, False)
                selector.register(pipe, selectors.EVENT_READ, descriptor)
        except BaseException:
            signal_group(process, signal.SIGKILL)
            for pipe, _capture, _size in streams.values():
                pipe.close()
            selector.close()
            raise
        started = time.monotonic()
        deadline = started + timeout
        termination = None
        term_deadline = None
        hard_deadline = None
        exited_deadline = None

        def begin_termination(reason, now):
            nonlocal termination, term_deadline
            if termination is None:
                termination = reason
                signal_group(process, signal.SIGTERM)
                term_deadline = now + 0.25

        try:
            while True:
                now = time.monotonic()
                returncode = process.poll()
                if returncode is not None and exited_deadline is None:
                    # A daemonized descendant may retain an inherited pipe after
                    # the direct child exits. Drain briefly, then close our read
                    # ends instead of waiting for a pipe EOF which may never come.
                    exited_deadline = now + 0.10
                if termination is None and returncode is None and now >= deadline:
                    begin_termination("timeout", now)
                if term_deadline is not None and hard_deadline is None and now >= term_deadline:
                    signal_group(process, signal.SIGKILL)
                    hard_deadline = now + 0.25
                if not streams:
                    if termination is None and returncode is not None:
                        break
                if (
                    termination is None
                    and exited_deadline is not None
                    and now >= exited_deadline
                ):
                    break
                if hard_deadline is not None and now >= hard_deadline:
                    break

                wakeups = [now + 0.05]
                if termination is None and returncode is None:
                    wakeups.append(deadline)
                if term_deadline is not None and hard_deadline is None:
                    wakeups.append(term_deadline)
                if exited_deadline is not None:
                    wakeups.append(exited_deadline)
                if hard_deadline is not None:
                    wakeups.append(hard_deadline)
                wait = max(0.0, min(wakeups) - now)
                for key, _events in selector.select(wait):
                    descriptor = key.data
                    entry = streams.get(descriptor)
                    if entry is None:
                        continue
                    pipe, capture, captured = entry
                    while True:
                        try:
                            chunk = os.read(descriptor, 64 * 1024)
                        except BlockingIOError:
                            break
                        except OSError:
                            chunk = b""
                        if not chunk:
                            try:
                                selector.unregister(pipe)
                            except Exception:
                                pass
                            pipe.close()
                            streams.pop(descriptor, None)
                            break
                        remaining = output_limit - captured
                        if remaining > 0:
                            retained = chunk[:remaining]
                            capture.write(retained)
                            captured += len(retained)
                            entry[2] = captured
                        if len(chunk) > max(remaining, 0):
                            begin_termination("output", time.monotonic())
        finally:
            if process.poll() is None:
                # Selector/tempfile failures must not turn a bounded helper
                # into an orphaned unbounded subprocess.
                signal_group(process, signal.SIGKILL)
            for pipe, _capture, _captured in list(streams.values()):
                try:
                    selector.unregister(pipe)
                except Exception:
                    pass
                pipe.close()
            selector.close()
        process.poll()
        stdout_file.seek(0)
        stderr_file.seek(0)
        return (
            process.returncode,
            stdout_file.read(output_limit),
            stderr_file.read(output_limit),
            termination == "timeout",
            termination == "output",
            None,
        )
    finally:
        stdout_file.close()
        stderr_file.close()


returncode, stdout, stderr, timed_out, output_exceeded, start_error = run_capped()
if start_error is not None:
    print(
        "could not start bounded command %s: %s"
        % (os.path.basename(argv[0]), start_error),
        file=sys.stderr,
    )
    raise SystemExit(127)

if stdout:
    sys.stdout.buffer.write(stdout)
if stderr:
    sys.stderr.buffer.write(stderr)
if output_exceeded:
    print(
        "bounded command output exceeded %d bytes per stream: %s"
        % (output_limit, os.path.basename(argv[0])),
        file=sys.stderr,
    )
    raise SystemExit(125)
if timed_out:
    print("bounded command timed out: %s" % os.path.basename(argv[0]), file=sys.stderr)
    raise SystemExit(124)
if returncode is None:
    print("bounded command could not be reaped: %s" % os.path.basename(argv[0]), file=sys.stderr)
    raise SystemExit(124)
returncode = returncode or 0
if returncode < 0:
    returncode = 128 + (-returncode)
raise SystemExit(min(returncode, 255))
PY
}

# Retry one argv under a single Python monotonic clock. Each attempt has its own
# process-group timeout and the whole retry window has one absolute deadline.
mac_retry_bounded() {
  local total_timeout="$1" command_timeout="$2" interval="$3" python_bin=""
  local output_limit=""
  shift 3
  [ "$#" -gt 0 ] || return 2
  python_bin="$(mac_launchd_python_bin)" || return $?
  output_limit="${MAC_LAUNCHD_MAX_OUTPUT_BYTES:-1048576}"
  "$python_bin" - "$total_timeout" "$command_timeout" "$interval" \
    "$output_limit" "$@" <<'PY'
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

try:
    total, per_command, interval = map(float, sys.argv[1:4])
except (TypeError, ValueError):
    raise SystemExit("invalid bounded-retry timeout")
if not all(math.isfinite(value) for value in (total, per_command, interval)):
    raise SystemExit("bounded-retry timeouts must be finite")
if total <= 0 or per_command <= 0 or interval < 0:
    raise SystemExit("bounded-retry timeouts must be positive")
try:
    output_limit = int(sys.argv[4])
except (TypeError, ValueError):
    raise SystemExit("invalid bounded-retry output limit")
if output_limit <= 0 or output_limit > 16 * 1024 * 1024:
    raise SystemExit("bounded-retry output limit is out of range")
argv = sys.argv[5:]
deadline = time.monotonic() + total
last_stdout = b""
last_stderr = b""


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except OSError:
        pass


def run_capped(timeout):
    stdout_file = tempfile.TemporaryFile()
    stderr_file = tempfile.TemporaryFile()
    try:
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            return None, b"", b"", False, False, exc
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        streams = {
            process.stdout.fileno(): [process.stdout, stdout_file, 0],
            process.stderr.fileno(): [process.stderr, stderr_file, 0],
        }
        try:
            for descriptor, (pipe, _capture, _size) in streams.items():
                os.set_blocking(descriptor, False)
                selector.register(pipe, selectors.EVENT_READ, descriptor)
        except BaseException:
            signal_group(process, signal.SIGKILL)
            for pipe, _capture, _size in streams.values():
                pipe.close()
            selector.close()
            raise
        command_deadline = time.monotonic() + timeout
        termination = None
        term_deadline = None
        hard_deadline = None
        exited_deadline = None

        def begin_termination(reason, now):
            nonlocal termination, term_deadline
            if termination is None:
                termination = reason
                signal_group(process, signal.SIGTERM)
                term_deadline = now + 0.25

        try:
            while True:
                now = time.monotonic()
                returncode = process.poll()
                if returncode is not None and exited_deadline is None:
                    exited_deadline = now + 0.10
                if (
                    termination is None
                    and returncode is None
                    and now >= command_deadline
                ):
                    begin_termination("timeout", now)
                if term_deadline is not None and hard_deadline is None and now >= term_deadline:
                    signal_group(process, signal.SIGKILL)
                    hard_deadline = now + 0.25
                if not streams and termination is None and returncode is not None:
                    break
                if (
                    termination is None
                    and exited_deadline is not None
                    and now >= exited_deadline
                ):
                    break
                if hard_deadline is not None and now >= hard_deadline:
                    break

                wakeups = [now + 0.05]
                if termination is None and returncode is None:
                    wakeups.append(command_deadline)
                if term_deadline is not None and hard_deadline is None:
                    wakeups.append(term_deadline)
                if exited_deadline is not None and termination is None:
                    wakeups.append(exited_deadline)
                if hard_deadline is not None:
                    wakeups.append(hard_deadline)
                wait = max(0.0, min(wakeups) - now)
                for key, _events in selector.select(wait):
                    descriptor = key.data
                    entry = streams.get(descriptor)
                    if entry is None:
                        continue
                    pipe, capture, captured = entry
                    while True:
                        try:
                            chunk = os.read(descriptor, 64 * 1024)
                        except BlockingIOError:
                            break
                        except OSError:
                            chunk = b""
                        if not chunk:
                            try:
                                selector.unregister(pipe)
                            except Exception:
                                pass
                            pipe.close()
                            streams.pop(descriptor, None)
                            break
                        remaining = output_limit - captured
                        if remaining > 0:
                            retained = chunk[:remaining]
                            capture.write(retained)
                            captured += len(retained)
                            entry[2] = captured
                        if len(chunk) > max(remaining, 0):
                            begin_termination("output", time.monotonic())
        finally:
            if process.poll() is None:
                signal_group(process, signal.SIGKILL)
            for pipe, _capture, _captured in list(streams.values()):
                try:
                    selector.unregister(pipe)
                except Exception:
                    pass
                pipe.close()
            selector.close()
        process.poll()
        stdout_file.seek(0)
        stderr_file.seek(0)
        return (
            process.returncode,
            stdout_file.read(output_limit),
            stderr_file.read(output_limit),
            termination == "timeout",
            termination == "output",
            None,
        )
    finally:
        stdout_file.close()
        stderr_file.close()

while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        if last_stdout:
            sys.stdout.buffer.write(last_stdout)
        if last_stderr:
            sys.stderr.buffer.write(last_stderr)
        print("bounded retry deadline expired: %s" % os.path.basename(argv[0]), file=sys.stderr)
        raise SystemExit(124)
    returncode, stdout, stderr, timed_out, output_exceeded, start_error = run_capped(
        min(per_command, remaining)
    )
    if start_error is not None:
        print(
            "could not start bounded command %s: %s"
            % (os.path.basename(argv[0]), start_error),
            file=sys.stderr,
        )
        raise SystemExit(127)
    last_stdout = stdout or b""
    last_stderr = stderr or b""
    if output_exceeded:
        if last_stdout:
            sys.stdout.buffer.write(last_stdout)
        if last_stderr:
            sys.stderr.buffer.write(last_stderr)
        print(
            "bounded retry output exceeded %d bytes per stream: %s"
            % (output_limit, os.path.basename(argv[0])),
            file=sys.stderr,
        )
        raise SystemExit(125)
    if returncode == 0 and not timed_out:
        if last_stdout:
            sys.stdout.buffer.write(last_stdout)
        if last_stderr:
            sys.stderr.buffer.write(last_stderr)
        raise SystemExit(0)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(interval, remaining))
PY
}

mac_launchd_job_state() {
  local target="$1" display_label="$2" mode="${3:-user}" output="" rc=0
  mac_launchd_validate_mode "$mode" || return $?
  output="$(mac_launchd_run_control_bounded "$mode" \
    "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
    print "$target" 2>&1)" || rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '%s\n' active
    return 0
  fi
  if [ "$rc" -eq 113 ]; then
    case "$output" in
      *"Could not find service"*)
        printf '%s\n' inactive
        return 0
        ;;
    esac
  fi
  if [ "$rc" -eq 124 ]; then
    mac_launchd_error "timed out inspecting launchd job $display_label"
  else
    mac_launchd_error \
      "could not inspect launchd job $display_label (exit $rc): $output"
  fi
  return 2
}

mac_launchd_wait_unloaded() {
  local target="$1" display_label="$2" mode="${3:-user}" python_bin=""
  local output_limit=""
  mac_launchd_validate_mode "$mode" || return $?
  python_bin="$(mac_launchd_python_bin)" || return $?
  output_limit="${MAC_LAUNCHD_MAX_OUTPUT_BYTES:-1048576}"
  "$python_bin" - \
    "${MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS:-45}" \
    "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
    "${MAC_LAUNCHD_POLL_INTERVAL_SECONDS:-1}" \
    "$output_limit" \
    "$target" "$display_label" "${MAC_LAUNCHD_LOG_PREFIX:-[launchd]}" \
    "$mode" <<'PY'
import math
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time

try:
    total, per_command, interval = map(float, sys.argv[1:4])
except (TypeError, ValueError):
    raise SystemExit("invalid launchd unload timeout")
if not all(math.isfinite(value) for value in (total, per_command, interval)):
    raise SystemExit("launchd unload timeouts must be finite")
if total <= 0 or per_command <= 0 or interval < 0:
    raise SystemExit("launchd unload timeouts must be positive")
try:
    output_limit = int(sys.argv[4])
except (TypeError, ValueError):
    raise SystemExit("invalid launchd unload output limit")
if output_limit <= 0 or output_limit > 16 * 1024 * 1024:
    raise SystemExit("launchd unload output limit is out of range")
target, label, prefix, mode = sys.argv[5:9]
deadline = time.monotonic() + total
control_argv = ["launchctl"] if mode == "user" else ["sudo", "-n", "launchctl"]


def signal_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except OSError:
        pass


def run_print(timeout):
    output_file = tempfile.TemporaryFile()
    try:
        try:
            process = subprocess.Popen(
                control_argv + ["print", target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return 127, "could not start launchctl: %s" % exc, False, False
        assert process.stdout is not None
        selector = selectors.DefaultSelector()
        descriptor = process.stdout.fileno()
        try:
            os.set_blocking(descriptor, False)
            selector.register(process.stdout, selectors.EVENT_READ)
        except BaseException:
            signal_group(process, signal.SIGKILL)
            process.stdout.close()
            selector.close()
            raise
        command_deadline = time.monotonic() + timeout
        captured = 0
        pipe_open = True
        termination = None
        term_deadline = None
        hard_deadline = None
        exited_deadline = None

        def begin_termination(reason, now):
            nonlocal termination, term_deadline
            if termination is None:
                termination = reason
                signal_group(process, signal.SIGTERM)
                term_deadline = now + 0.25

        try:
            while True:
                now = time.monotonic()
                returncode = process.poll()
                if returncode is not None and exited_deadline is None:
                    exited_deadline = now + 0.10
                if (
                    termination is None
                    and returncode is None
                    and now >= command_deadline
                ):
                    begin_termination("timeout", now)
                if term_deadline is not None and hard_deadline is None and now >= term_deadline:
                    signal_group(process, signal.SIGKILL)
                    hard_deadline = now + 0.25
                if not pipe_open and termination is None and returncode is not None:
                    break
                if (
                    termination is None
                    and exited_deadline is not None
                    and now >= exited_deadline
                ):
                    break
                if hard_deadline is not None and now >= hard_deadline:
                    break

                wakeups = [now + 0.05]
                if termination is None and returncode is None:
                    wakeups.append(command_deadline)
                if term_deadline is not None and hard_deadline is None:
                    wakeups.append(term_deadline)
                if exited_deadline is not None and termination is None:
                    wakeups.append(exited_deadline)
                if hard_deadline is not None:
                    wakeups.append(hard_deadline)
                wait = max(0.0, min(wakeups) - now)
                for _key, _events in selector.select(wait):
                    while True:
                        try:
                            chunk = os.read(descriptor, 64 * 1024)
                        except BlockingIOError:
                            break
                        except OSError:
                            chunk = b""
                        if not chunk:
                            selector.unregister(process.stdout)
                            process.stdout.close()
                            pipe_open = False
                            break
                        remaining = output_limit - captured
                        if remaining > 0:
                            retained = chunk[:remaining]
                            output_file.write(retained)
                            captured += len(retained)
                        if len(chunk) > max(remaining, 0):
                            begin_termination("output", time.monotonic())
        finally:
            if process.poll() is None:
                signal_group(process, signal.SIGKILL)
            if pipe_open:
                try:
                    selector.unregister(process.stdout)
                except Exception:
                    pass
                process.stdout.close()
            selector.close()
        process.poll()
        output_file.seek(0)
        output = output_file.read(output_limit).decode("utf-8", errors="replace")
        return (
            process.returncode,
            output,
            termination == "timeout",
            termination == "output",
        )
    finally:
        output_file.close()


while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        print("%s ERROR: launchd job remained loaded after bootout: %s" % (prefix, label), file=sys.stderr)
        raise SystemExit(1)
    deadline_limited = remaining <= per_command
    rc, output, timed_out, output_exceeded = run_print(min(per_command, remaining))
    if output_exceeded:
        print(
            "%s ERROR: launchd inspection output exceeded %d bytes: %s"
            % (prefix, output_limit, label),
            file=sys.stderr,
        )
        raise SystemExit(2)
    if timed_out:
        if deadline_limited:
            print("%s ERROR: launchd job remained loaded after bootout: %s" % (prefix, label), file=sys.stderr)
            raise SystemExit(1)
        print("%s ERROR: timed out inspecting launchd job %s" % (prefix, label), file=sys.stderr)
        raise SystemExit(2)
    if rc == 113 and "Could not find service" in output:
        raise SystemExit(0)
    if rc != 0:
        if rc is None:
            rc = 124
        print("%s ERROR: could not inspect launchd job %s (exit %s): %s" % (prefix, label, rc, output.rstrip()), file=sys.stderr)
        raise SystemExit(2)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(interval, remaining))
PY
}

mac_launchd_stop_job_if_present() {
  local target="$1" display_label="$2" mode="${3:-user}" state=""
  local bootout_output="" bootout_rc=0 wait_rc=0 probe_rc=0
  mac_launchd_validate_mode "$mode" || return $?
  state="$(mac_launchd_job_state "$target" "$display_label" "$mode")" \
    || probe_rc=$?
  [ "$probe_rc" -eq 0 ] || return "$probe_rc"
  [ "$state" = active ] || return 0
  bootout_output="$(mac_launchd_run_control_bounded "$mode" \
    "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
    bootout "$target" 2>&1)" || bootout_rc=$?
  mac_launchd_wait_unloaded "$target" "$display_label" "$mode" || wait_rc=$?
  if [ "$wait_rc" -ne 0 ]; then
    if [ "$bootout_rc" -ne 0 ]; then
      mac_launchd_error \
        "launchctl bootout failed for $display_label (exit $bootout_rc): $bootout_output"
    fi
    return "$wait_rc"
  fi
}

mac_launchd_bootstrap_job() {
  local domain="$1" plist="$2" target="$3" display_label="$4"
  local mode="${5:-user}" state="" output="" rc=0
  mac_launchd_validate_mode "$mode" || return $?
  state="$(mac_launchd_job_state "$target" "$display_label" "$mode")" \
    || return $?
  if [ "$state" != inactive ]; then
    mac_launchd_error \
      "refusing to bootstrap launchd job before prior generation is absent: $display_label"
    return 1
  fi
  output="$(mac_launchd_run_control_bounded "$mode" \
    "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
    enable "$target" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    mac_launchd_error \
      "launchctl enable failed for $display_label (exit $rc): $output"
    return 1
  fi
  output=""
  rc=0
  output="$(mac_launchd_run_control_bounded "$mode" \
    "${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}" \
    bootstrap "$domain" "$plist" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    mac_launchd_error \
      "launchctl bootstrap failed for $display_label (exit $rc): $output"
    return 1
  fi
  state="$(mac_launchd_job_state "$target" "$display_label" "$mode")" \
    || return $?
  if [ "$state" != active ]; then
    mac_launchd_error \
      "launchd job is not loaded after bootstrap: $display_label"
    return 1
  fi
}

mac_launchd_artifact_timeout() {
  printf '%s\n' \
    "${MAC_LAUNCHD_ARTIFACT_TIMEOUT_SECONDS:-${MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS:-10}}"
}

mac_launchd_create_transaction_directory() {
  local parent="$1" display_label="$2" mode="${3:-user}" program=""
  program='
import os
import sys
import tempfile
from pathlib import Path

parent = Path(sys.argv[1])
label = "".join(character if character.isalnum() or character in "._-" else "_" for character in sys.argv[2])
path = Path(tempfile.mkdtemp(prefix=".%s.rollback." % label, dir=str(parent)))
os.chmod(path, 0o700)
directory_fd = os.open(str(parent), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print(path)
'
  mac_launchd_run_python_bounded \
    "$mode" "$(mac_launchd_artifact_timeout)" "$program" \
    "$parent" "$display_label"
}

mac_launchd_snapshot_file() {
  local source="$1" backup="$2" mode="${3:-user}" program=""
  program='
import hashlib
import os
import stat
import sys
from pathlib import Path

source = Path(sys.argv[1])
backup = Path(sys.argv[2])


def identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


try:
    initial = source.lstat()
except FileNotFoundError:
    print("0")
    raise SystemExit(0)
if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
    raise SystemExit("refusing non-regular or symlink lifecycle artifact: %s" % source)
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
source_fd = os.open(str(source), flags)
destination_fd = -1
destination_inode = None
completed = False
try:
    opened = os.fstat(source_fd)
    if not stat.S_ISREG(opened.st_mode) or identity(initial) != identity(opened):
        raise SystemExit("lifecycle artifact changed while opening: %s" % source)
    destination_fd = os.open(
        str(backup), os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
    )
    created = os.fstat(destination_fd)
    destination_inode = (created.st_dev, created.st_ino)
    source_hash = hashlib.sha256()
    while True:
        chunk = os.read(source_fd, 64 * 1024)
        if not chunk:
            break
        source_hash.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination_fd, view)
            if written <= 0:
                raise OSError("short lifecycle snapshot write")
            view = view[written:]
    after = os.fstat(source_fd)
    try:
        current = source.lstat()
    except FileNotFoundError:
        raise SystemExit("lifecycle artifact was renamed while snapshotting: %s" % source)
    if identity(opened) != identity(after) or identity(opened) != identity(current):
        raise SystemExit("lifecycle artifact changed while snapshotting: %s" % source)
    if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
        raise SystemExit("refusing non-regular lifecycle artifact: %s" % source)
    if os.geteuid() == 0:
        os.fchown(destination_fd, opened.st_uid, opened.st_gid)
    os.fchmod(destination_fd, stat.S_IMODE(opened.st_mode))
    os.fsync(destination_fd)
    published = os.fstat(destination_fd)
    try:
        current_backup = backup.lstat()
    except FileNotFoundError:
        raise SystemExit("lifecycle artifact snapshot disappeared: %s" % backup)
    if (
        not stat.S_ISREG(current_backup.st_mode)
        or stat.S_ISLNK(current_backup.st_mode)
        or identity(published) != identity(current_backup)
    ):
        raise SystemExit("lifecycle artifact snapshot path changed: %s" % backup)
    readback_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        readback_flags |= os.O_NOFOLLOW
    readback_fd = os.open(str(backup), readback_flags)
    backup_hash = hashlib.sha256()
    backup_size = 0
    try:
        readback_opened = os.fstat(readback_fd)
        if (
            not stat.S_ISREG(readback_opened.st_mode)
            or identity(published) != identity(readback_opened)
        ):
            raise SystemExit("lifecycle artifact snapshot changed before read-back: %s" % backup)
        while True:
            chunk = os.read(readback_fd, 64 * 1024)
            if not chunk:
                break
            backup_hash.update(chunk)
            backup_size += len(chunk)
        readback_after = os.fstat(readback_fd)
    finally:
        os.close(readback_fd)
    try:
        current_backup = backup.lstat()
    except FileNotFoundError:
        raise SystemExit("lifecycle artifact snapshot disappeared during read-back: %s" % backup)
    if (
        identity(readback_opened) != identity(readback_after)
        or identity(readback_opened) != identity(current_backup)
        or not stat.S_ISREG(current_backup.st_mode)
        or stat.S_ISLNK(current_backup.st_mode)
    ):
        raise SystemExit("lifecycle artifact snapshot changed during read-back: %s" % backup)
    if backup_size != opened.st_size or backup_hash.digest() != source_hash.digest():
        raise SystemExit("lifecycle artifact snapshot read-back mismatch: %s" % source)
    completed = True
finally:
    os.close(source_fd)
    if destination_fd >= 0:
        os.close(destination_fd)
    if not completed and destination_inode is not None:
        try:
            failed_backup = backup.lstat()
        except FileNotFoundError:
            pass
        else:
            if (failed_backup.st_dev, failed_backup.st_ino) == destination_inode:
                backup.unlink()
directory_fd = os.open(str(backup.parent), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
print("1")
'
  mac_launchd_run_python_bounded \
    "$mode" "$(mac_launchd_artifact_timeout)" "$program" \
    "$source" "$backup"
}

mac_launchd_copy_replace() {
  local source="$1" destination="$2" mode="${3:-user}"
  local requested_mode="${4:-}" requested_uid="${5:-}" requested_gid="${6:-}"
  local consume_source="${7:-0}" preserve_source_owner="${8:-0}" program=""
  program='
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
(
    execution_mode,
    requested_mode,
    requested_uid,
    requested_gid,
    consume_source,
    preserve_source_owner,
) = sys.argv[3:9]


def identity(metadata):
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


initial = source.lstat()
if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
    raise SystemExit("source lifecycle artifact is not a regular file: %s" % source)
if execution_mode == "user" and consume_source == "1" and source.parent.resolve() != destination.parent.resolve():
    raise SystemExit("staged lifecycle artifact must share the destination directory")
try:
    old_metadata = destination.lstat()
except FileNotFoundError:
    old_metadata = None
if old_metadata is not None and (stat.S_ISLNK(old_metadata.st_mode) or not stat.S_ISREG(old_metadata.st_mode)):
    raise SystemExit("destination lifecycle artifact is not a regular file: %s" % destination)
flags = os.O_RDONLY
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
source_fd = os.open(str(source), flags)
temporary_fd, raw_temporary = tempfile.mkstemp(prefix=".%s.stage." % destination.name, dir=str(destination.parent))
temporary = Path(raw_temporary)
created_temporary = os.fstat(temporary_fd)
temporary_inode = (created_temporary.st_dev, created_temporary.st_ino)
replaced = False
try:
    opened = os.fstat(source_fd)
    if not stat.S_ISREG(opened.st_mode) or identity(initial) != identity(opened):
        raise SystemExit("source lifecycle artifact changed while opening: %s" % source)
    file_mode = int(requested_mode, 8) if requested_mode else stat.S_IMODE(opened.st_mode)
    if file_mode < 0 or file_mode > 0o7777:
        raise SystemExit("invalid lifecycle artifact mode")
    if requested_uid:
        owner = int(requested_uid)
    elif preserve_source_owner == "1":
        owner = opened.st_uid
    elif execution_mode == "system":
        owner = old_metadata.st_uid if old_metadata is not None else 0
    else:
        owner = opened.st_uid
    if requested_gid:
        group = int(requested_gid)
    elif preserve_source_owner == "1":
        group = opened.st_gid
    elif execution_mode == "system":
        group = old_metadata.st_gid if old_metadata is not None else 0
    else:
        group = opened.st_gid

    source_hash = hashlib.sha256()
    while True:
        chunk = os.read(source_fd, 64 * 1024)
        if not chunk:
            break
        source_hash.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(temporary_fd, view)
            if written <= 0:
                raise OSError("short lifecycle replacement write")
            view = view[written:]
    after = os.fstat(source_fd)
    try:
        current_source = source.lstat()
    except FileNotFoundError:
        raise SystemExit("source lifecycle artifact was renamed while copying: %s" % source)
    if (
        identity(opened) != identity(after)
        or identity(opened) != identity(current_source)
        or stat.S_ISLNK(current_source.st_mode)
        or not stat.S_ISREG(current_source.st_mode)
    ):
        raise SystemExit("source lifecycle artifact changed while copying: %s" % source)

    if os.geteuid() == 0:
        os.fchown(temporary_fd, owner, group)
    elif owner != os.geteuid() or group != os.getegid():
        raise SystemExit("privileged lifecycle ownership change did not run as root")
    os.fchmod(temporary_fd, file_mode)
    os.fsync(temporary_fd)

    temporary_metadata = os.fstat(temporary_fd)
    try:
        current_temporary = temporary.lstat()
    except FileNotFoundError:
        raise SystemExit("lifecycle replacement staging path disappeared: %s" % temporary)
    if (
        not stat.S_ISREG(current_temporary.st_mode)
        or stat.S_ISLNK(current_temporary.st_mode)
        or identity(temporary_metadata) != identity(current_temporary)
    ):
        raise SystemExit("lifecycle replacement staging path changed: %s" % temporary)
    readback_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        readback_flags |= os.O_NOFOLLOW
    readback_fd = os.open(str(temporary), readback_flags)
    temporary_hash = hashlib.sha256()
    temporary_size = 0
    try:
        readback_opened = os.fstat(readback_fd)
        if (
            not stat.S_ISREG(readback_opened.st_mode)
            or identity(temporary_metadata) != identity(readback_opened)
        ):
            raise SystemExit("lifecycle replacement changed before read-back: %s" % temporary)
        while True:
            chunk = os.read(readback_fd, 64 * 1024)
            if not chunk:
                break
            temporary_hash.update(chunk)
            temporary_size += len(chunk)
        readback_after = os.fstat(readback_fd)
    finally:
        os.close(readback_fd)
    try:
        current_temporary = temporary.lstat()
    except FileNotFoundError:
        raise SystemExit("lifecycle replacement disappeared during read-back: %s" % temporary)
    if (
        identity(readback_opened) != identity(readback_after)
        or identity(readback_opened) != identity(current_temporary)
        or not stat.S_ISREG(current_temporary.st_mode)
        or stat.S_ISLNK(current_temporary.st_mode)
    ):
        raise SystemExit("lifecycle replacement changed during read-back: %s" % temporary)
    if temporary_size != opened.st_size or temporary_hash.digest() != source_hash.digest():
        raise SystemExit("lifecycle replacement read-back mismatch: %s" % source)

    try:
        current_destination = destination.lstat()
    except FileNotFoundError:
        current_destination = None
    if old_metadata is None:
        if current_destination is not None:
            raise SystemExit("destination lifecycle artifact appeared during replacement: %s" % destination)
    elif (
        current_destination is None
        or stat.S_ISLNK(current_destination.st_mode)
        or not stat.S_ISREG(current_destination.st_mode)
        or identity(old_metadata) != identity(current_destination)
    ):
        raise SystemExit("destination lifecycle artifact changed during replacement: %s" % destination)
    current_temporary = temporary.lstat()
    if identity(temporary_metadata) != identity(current_temporary):
        raise SystemExit("lifecycle replacement staging path changed before install: %s" % temporary)
    os.replace(temporary, destination)
    replaced = True
    installed = os.fstat(temporary_fd)
    try:
        current_destination = destination.lstat()
    except FileNotFoundError:
        raise SystemExit("destination lifecycle artifact disappeared after replacement: %s" % destination)
    if (
        not stat.S_ISREG(current_destination.st_mode)
        or stat.S_ISLNK(current_destination.st_mode)
        or identity(installed) != identity(current_destination)
    ):
        raise SystemExit("destination lifecycle artifact changed during installation: %s" % destination)
    directory_fd = os.open(str(destination.parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
finally:
    os.close(source_fd)
    os.close(temporary_fd)
    if not replaced:
        try:
            failed_temporary = temporary.lstat()
        except FileNotFoundError:
            pass
        else:
            if (failed_temporary.st_dev, failed_temporary.st_ino) == temporary_inode:
                temporary.unlink()
if consume_source == "1":
    try:
        final_source = source.lstat()
    except FileNotFoundError:
        raise SystemExit("staged lifecycle artifact disappeared before cleanup: %s" % source)
    if identity(final_source) != identity(opened):
        raise SystemExit("staged lifecycle artifact changed before cleanup: %s" % source)
    try:
        source.unlink()
    except FileNotFoundError:
        pass
    source_directory_fd = os.open(str(source.parent), os.O_RDONLY)
    try:
        os.fsync(source_directory_fd)
    finally:
        os.close(source_directory_fd)
'
  mac_launchd_run_python_bounded \
    "$mode" "$(mac_launchd_artifact_timeout)" "$program" \
    "$source" "$destination" "$mode" \
    "$requested_mode" "$requested_uid" "$requested_gid" "$consume_source" \
    "$preserve_source_owner"
}

mac_launchd_atomic_replace() {
  local staged="$1" destination="$2" mode="${3:-user}"
  local requested_mode="${4:-}" requested_uid="${5:-}" requested_gid="${6:-}"
  mac_launchd_copy_replace \
    "$staged" "$destination" "$mode" \
    "$requested_mode" "$requested_uid" "$requested_gid" 1
}

mac_launchd_atomic_restore() {
  local source="$1" destination="$2" mode="${3:-user}"
  mac_launchd_copy_replace "$source" "$destination" "$mode" "" "" "" 0 1
}

mac_launchd_remove_file_and_fsync() {
  local path="$1" mode="${2:-user}" program=""
  program='
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    metadata = path.lstat()
except FileNotFoundError:
    raise SystemExit(0)
if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
    raise SystemExit("refusing to unlink lifecycle artifact directory: %s" % path)
path.unlink()
directory_fd = os.open(str(path.parent), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
'
  mac_launchd_run_python_bounded \
    "$mode" "$(mac_launchd_artifact_timeout)" "$program" "$path"
}

mac_launchd_fsync_directory() {
  local path="$1" mode="${2:-user}" program=""
  program='
import os
import sys
from pathlib import Path

directory_fd = os.open(str(Path(sys.argv[1])), os.O_RDONLY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
'
  mac_launchd_run_python_bounded \
    "$mode" "$(mac_launchd_artifact_timeout)" "$program" "$path"
}

mac_launchd_cleanup_transaction_artifacts() {
  local mode="$1" transaction_dir="$2" program=""
  shift 2
  program='
import os
import shutil
import stat
import sys
from pathlib import Path

transaction_dir = Path(sys.argv[1])
temporaries = [Path(value) for value in sys.argv[2:]]
parents = set()
for temporary in temporaries:
    try:
        metadata = temporary.lstat()
    except FileNotFoundError:
        continue
    if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
        raise SystemExit("refusing temporary lifecycle artifact directory: %s" % temporary)
    temporary.unlink()
    parents.add(temporary.parent)
try:
    transaction_metadata = transaction_dir.lstat()
except FileNotFoundError:
    transaction_metadata = None
if transaction_metadata is not None:
    if stat.S_ISLNK(transaction_metadata.st_mode) or not stat.S_ISDIR(transaction_metadata.st_mode):
        raise SystemExit("invalid lifecycle transaction directory: %s" % transaction_dir)
    shutil.rmtree(transaction_dir)
    parents.add(transaction_dir.parent)
for parent in parents:
    directory_fd = os.open(str(parent), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
'
  mac_launchd_run_python_bounded \
    "$mode" "$(mac_launchd_artifact_timeout)" "$program" \
    "$transaction_dir" "$@"
}

MAC_LAUNCHD_TX_ACTIVE=0
MAC_LAUNCHD_TX_MUTATING=0
MAC_LAUNCHD_TX_COUNT=0
MAC_LAUNCHD_TX_TEMP_COUNT=0
MAC_LAUNCHD_TX_DIR=""
MAC_LAUNCHD_TX_DOMAIN=""
MAC_LAUNCHD_TX_TARGET=""
MAC_LAUNCHD_TX_LABEL=""
MAC_LAUNCHD_TX_MODE="user"
MAC_LAUNCHD_TX_OBSERVED_STATE=""
MAC_LAUNCHD_TX_OLD_STATE=""
MAC_LAUNCHD_TX_ROLLBACK_HOOK=""
MAC_LAUNCHD_TX_AFTER_RESTORE_HOOK=""
MAC_LAUNCHD_TX_SAVED_EXIT_TRAP=""
MAC_LAUNCHD_TX_SAVED_HUP_TRAP=""
MAC_LAUNCHD_TX_SAVED_INT_TRAP=""
MAC_LAUNCHD_TX_SAVED_TERM_TRAP=""
MAC_LAUNCHD_TX_PATHS=()
MAC_LAUNCHD_TX_BACKUPS=()
MAC_LAUNCHD_TX_EXISTED=()
MAC_LAUNCHD_TX_TEMPORARIES=()

mac_launchd_transaction_save_traps() {
  MAC_LAUNCHD_TX_SAVED_EXIT_TRAP="$(trap -p EXIT)"
  MAC_LAUNCHD_TX_SAVED_HUP_TRAP="$(trap -p HUP)"
  MAC_LAUNCHD_TX_SAVED_INT_TRAP="$(trap -p INT)"
  MAC_LAUNCHD_TX_SAVED_TERM_TRAP="$(trap -p TERM)"
}

mac_launchd_transaction_restore_trap_definition() {
  local definition="$1"
  [ -z "$definition" ] || builtin source /dev/stdin <<<"$definition"
}

# An EXIT trap cannot be re-entered by restoring it while an EXIT trap is
# already running. Convert only the signal suffix emitted by `trap -p` to a
# source-scoped RETURN trap, then make a status-only helper the source's final
# command. Bash invokes the RETURN trap once with that command's exact status.
# This preserves the caller's function/global context (which a fresh shell
# would lose) without evaluating caller text.
mac_launchd_transaction_return_status() {
  return "$1"
}

mac_launchd_transaction_run_saved_exit_trap() {
  local definition="$1" original_rc="$2" return_definition="" trap_rc=0
  local restore_errexit=0
  [ -n "$definition" ] || return 0
  case "$original_rc" in
    ''|*[!0-9]*)
      mac_launchd_error "saved EXIT trap status is invalid"
      return 1
      ;;
  esac
  if [ "$original_rc" -gt 255 ]; then
    mac_launchd_error "saved EXIT trap status is outside the shell range"
    return 1
  fi
  case "$definition" in
    "trap -- "*" EXIT") ;;
    *)
      mac_launchd_error "saved EXIT trap has an unexpected definition"
      return 1
      ;;
  esac
  return_definition="${definition% EXIT} RETURN"
  trap - RETURN
  # Bash 5.2 can corrupt its function-variable stack when a RETURN trap calls
  # a function containing `local` while the sourced command is in an errexit-
  # suppressed `||` context. Temporarily disabling errexit and capturing the
  # status directly avoids that interpreter defect; restore the caller's mode
  # before returning from the bridge.
  case "$-" in
    *e*) restore_errexit=1; set +e ;;
  esac
  builtin source /dev/stdin \
    <<<"$return_definition"$'\nmac_launchd_transaction_return_status '"$original_rc"
  trap_rc=$?
  trap - RETURN
  [ "$restore_errexit" -eq 0 ] || set -e
  return "$trap_rc"
}

mac_launchd_transaction_restore_traps() {
  local exit_trap="$MAC_LAUNCHD_TX_SAVED_EXIT_TRAP"
  local hup_trap="$MAC_LAUNCHD_TX_SAVED_HUP_TRAP"
  local int_trap="$MAC_LAUNCHD_TX_SAVED_INT_TRAP"
  local term_trap="$MAC_LAUNCHD_TX_SAVED_TERM_TRAP"
  trap - EXIT HUP INT TERM
  MAC_LAUNCHD_TX_SAVED_EXIT_TRAP=""
  MAC_LAUNCHD_TX_SAVED_HUP_TRAP=""
  MAC_LAUNCHD_TX_SAVED_INT_TRAP=""
  MAC_LAUNCHD_TX_SAVED_TERM_TRAP=""
  mac_launchd_transaction_restore_trap_definition "$exit_trap"
  mac_launchd_transaction_restore_trap_definition "$hup_trap"
  mac_launchd_transaction_restore_trap_definition "$int_trap"
  mac_launchd_transaction_restore_trap_definition "$term_trap"
}

mac_launchd_transaction_track_file() {
  local path="$1" index=0 backup="" existed="" snapshot_rc=0
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || {
    mac_launchd_error "cannot track an artifact outside a launchd transaction"
    return 1
  }
  while [ "$index" -lt "$MAC_LAUNCHD_TX_COUNT" ]; do
    [ "${MAC_LAUNCHD_TX_PATHS[$index]}" != "$path" ] || return 0
    index=$(( index + 1 ))
  done
  backup="$MAC_LAUNCHD_TX_DIR/$MAC_LAUNCHD_TX_COUNT"
  existed="$(mac_launchd_snapshot_file \
    "$path" "$backup" "$MAC_LAUNCHD_TX_MODE")" || snapshot_rc=$?
  [ "$snapshot_rc" -eq 0 ] || return "$snapshot_rc"
  case "$existed" in
    0|1) ;;
    *)
      mac_launchd_error "invalid lifecycle snapshot result for $path: $existed"
      return 1
      ;;
  esac
  MAC_LAUNCHD_TX_PATHS[$MAC_LAUNCHD_TX_COUNT]="$path"
  MAC_LAUNCHD_TX_BACKUPS[$MAC_LAUNCHD_TX_COUNT]="$backup"
  MAC_LAUNCHD_TX_EXISTED[$MAC_LAUNCHD_TX_COUNT]="$existed"
  MAC_LAUNCHD_TX_COUNT=$(( MAC_LAUNCHD_TX_COUNT + 1 ))
}

mac_launchd_transaction_track_temporary() {
  local path="$1"
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || return 1
  MAC_LAUNCHD_TX_TEMPORARIES[$MAC_LAUNCHD_TX_TEMP_COUNT]="$path"
  MAC_LAUNCHD_TX_TEMP_COUNT=$(( MAC_LAUNCHD_TX_TEMP_COUNT + 1 ))
}

mac_launchd_transaction_cleanup() {
  local index=0 cleanup_rc=0
  while [ "$index" -lt "$MAC_LAUNCHD_TX_TEMP_COUNT" ]; do
    mac_launchd_remove_file_and_fsync \
      "${MAC_LAUNCHD_TX_TEMPORARIES[$index]}" \
      "$MAC_LAUNCHD_TX_MODE" || cleanup_rc=1
    index=$(( index + 1 ))
  done
  if [ -n "$MAC_LAUNCHD_TX_DIR" ]; then
    mac_launchd_cleanup_transaction_artifacts \
      "$MAC_LAUNCHD_TX_MODE" "$MAC_LAUNCHD_TX_DIR" || cleanup_rc=1
  fi
  return "$cleanup_rc"
}

mac_launchd_transaction_begin() {
  local domain="$1" plist="$2" target="$3" display_label="$4"
  local mode="${5:-user}" state="" begin_rc=0
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 0 ] || {
    mac_launchd_error "nested launchd transactions are not supported"
    return 1
  }
  mac_launchd_validate_mode "$mode" || return $?
  mac_launchd_transaction_save_traps
  state="$(mac_launchd_job_state "$target" "$display_label" "$mode")" \
    || {
      begin_rc=$?
      mac_launchd_transaction_restore_traps
      return "$begin_rc"
    }
  MAC_LAUNCHD_TX_DIR="$(mac_launchd_create_transaction_directory \
    "$(dirname "$plist")" "$display_label" "$mode")" || {
      begin_rc=$?
      mac_launchd_transaction_restore_traps
      return "$begin_rc"
    }
  MAC_LAUNCHD_TX_ACTIVE=1
  MAC_LAUNCHD_TX_MUTATING=0
  MAC_LAUNCHD_TX_COUNT=0
  MAC_LAUNCHD_TX_TEMP_COUNT=0
  MAC_LAUNCHD_TX_DOMAIN="$domain"
  MAC_LAUNCHD_TX_TARGET="$target"
  MAC_LAUNCHD_TX_LABEL="$display_label"
  MAC_LAUNCHD_TX_MODE="$mode"
  MAC_LAUNCHD_TX_OBSERVED_STATE="$state"
  MAC_LAUNCHD_TX_OLD_STATE="$state"
  MAC_LAUNCHD_TX_ROLLBACK_HOOK=""
  MAC_LAUNCHD_TX_AFTER_RESTORE_HOOK=""
  trap 'mac_launchd_transaction_on_exit "$?"' EXIT
  trap 'exit 129' HUP
  trap 'exit 130' INT
  trap 'exit 143' TERM
  mac_launchd_transaction_track_file "$plist" || begin_rc=$?
  if [ "$begin_rc" -ne 0 ]; then
    trap - EXIT HUP INT TERM
    MAC_LAUNCHD_TX_ACTIVE=0
    mac_launchd_transaction_cleanup || true
    mac_launchd_transaction_restore_traps
    return "$begin_rc"
  fi
  if [ "$state" = active ] && [ "${MAC_LAUNCHD_TX_EXISTED[0]}" -ne 1 ]; then
    mac_launchd_error \
      "cannot roll back active launchd job without its canonical plist: $display_label"
    trap - EXIT HUP INT TERM
    MAC_LAUNCHD_TX_ACTIVE=0
    mac_launchd_transaction_cleanup || true
    mac_launchd_transaction_restore_traps
    return 1
  fi
}

# A preceding quiescence phase may have stopped a job before this transaction
# can inspect it.  Callers with durable prestate may explicitly set the state
# that compensation must restore, without changing the state observed at begin
# or bypassing any absence proof in stop/bootstrap helpers.
mac_launchd_transaction_set_expected_prior_state() {
  local expected_state="$1"
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || {
    mac_launchd_error \
      "cannot set expected prior state outside a launchd transaction"
    return 1
  }
  [ "$MAC_LAUNCHD_TX_MUTATING" -eq 0 ] || {
    mac_launchd_error \
      "cannot set expected prior state after launchd mutation begins"
    return 1
  }
  case "$expected_state" in
    active)
      if [ "$MAC_LAUNCHD_TX_COUNT" -lt 1 ] \
        || [ "${MAC_LAUNCHD_TX_EXISTED[0]}" -ne 1 ]; then
        mac_launchd_error \
          "cannot expect active prior state without a canonical plist snapshot: $MAC_LAUNCHD_TX_LABEL"
        return 1
      fi
      ;;
    inactive) ;;
    *)
      mac_launchd_error \
        "invalid expected prior launchd state: $expected_state"
      return 2
      ;;
  esac
  MAC_LAUNCHD_TX_OLD_STATE="$expected_state"
}

mac_launchd_transaction_set_rollback_hook() {
  local function_name="$1"
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || return 1
  [ "$(type -t "$function_name" 2>/dev/null || true)" = function ] || {
    mac_launchd_error "rollback hook is not a shell function: $function_name"
    return 1
  }
  MAC_LAUNCHD_TX_ROLLBACK_HOOK="$function_name"
}

# Some migrations replace a job in one launchd domain with a job in another,
# or temporarily stop a separate supervisor job.  Its old artifact is restored
# by the transaction, but it must only be bootstrapped after every artifact and
# the primary old generation have been restored.  Keep this distinct from the
# pre-restore rollback hook used to withdraw daemon-owned resources.
mac_launchd_transaction_set_after_restore_hook() {
  local function_name="$1"
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || return 1
  [ "$(type -t "$function_name" 2>/dev/null || true)" = function ] || {
    mac_launchd_error "after-restore hook is not a shell function: $function_name"
    return 1
  }
  MAC_LAUNCHD_TX_AFTER_RESTORE_HOOK="$function_name"
}

mac_launchd_transaction_mark_mutating() {
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || return 1
  MAC_LAUNCHD_TX_MUTATING=1
}

mac_launchd_transaction_replace() {
  local staged="$1" destination="$2" requested_mode="${3:-}"
  local requested_uid="${4:-}" requested_gid="${5:-}" index=0 tracked=0
  local temporary_index=0 staged_tracked=0
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || return 1
  while [ "$index" -lt "$MAC_LAUNCHD_TX_COUNT" ]; do
    if [ "${MAC_LAUNCHD_TX_PATHS[$index]}" = "$destination" ]; then
      tracked=1
      break
    fi
    index=$(( index + 1 ))
  done
  [ "$tracked" -eq 1 ] || {
    mac_launchd_error "refusing to replace untracked lifecycle artifact: $destination"
    return 1
  }
  if [ "$MAC_LAUNCHD_TX_MODE" = system ]; then
    while [ "$temporary_index" -lt "$MAC_LAUNCHD_TX_TEMP_COUNT" ]; do
      if [ "${MAC_LAUNCHD_TX_TEMPORARIES[$temporary_index]}" = "$staged" ]; then
        staged_tracked=1
        break
      fi
      temporary_index=$(( temporary_index + 1 ))
    done
    [ "$staged_tracked" -eq 1 ] || {
      mac_launchd_error \
        "refusing privileged replacement from untracked staging artifact: $staged"
      return 1
    }
  fi
  mac_launchd_atomic_replace \
    "$staged" "$destination" "$MAC_LAUNCHD_TX_MODE" \
    "$requested_mode" "$requested_uid" "$requested_gid"
}

mac_launchd_transaction_rollback() {
  local rollback_rc=0 stop_rc=0 hook_rc=0 restore_rc=0 index=0 path=""
  local prior_active="$MAC_LAUNCHD_TX_ACTIVE"
  [ "$prior_active" -eq 1 ] || return 0
  trap - EXIT
  trap '' HUP INT TERM
  MAC_LAUNCHD_TX_ACTIVE=0
  if [ "$MAC_LAUNCHD_TX_MUTATING" -eq 1 ]; then
    mac_launchd_stop_job_if_present \
      "$MAC_LAUNCHD_TX_TARGET" "$MAC_LAUNCHD_TX_LABEL" \
      "$MAC_LAUNCHD_TX_MODE" || stop_rc=$?
    [ "$stop_rc" -eq 0 ] || rollback_rc=1
    if [ -n "$MAC_LAUNCHD_TX_ROLLBACK_HOOK" ]; then
      "$MAC_LAUNCHD_TX_ROLLBACK_HOOK" || hook_rc=$?
      [ "$hook_rc" -eq 0 ] || rollback_rc=1
    fi
    index=$(( MAC_LAUNCHD_TX_COUNT - 1 ))
    while [ "$index" -ge 0 ]; do
      path="${MAC_LAUNCHD_TX_PATHS[$index]}"
      restore_rc=0
      if [ "${MAC_LAUNCHD_TX_EXISTED[$index]}" -eq 1 ]; then
        mac_launchd_atomic_restore \
          "${MAC_LAUNCHD_TX_BACKUPS[$index]}" "$path" \
          "$MAC_LAUNCHD_TX_MODE" || restore_rc=$?
      else
        mac_launchd_remove_file_and_fsync \
          "$path" "$MAC_LAUNCHD_TX_MODE" || restore_rc=$?
      fi
      [ "$restore_rc" -eq 0 ] || rollback_rc=1
      index=$(( index - 1 ))
    done
    if [ "$MAC_LAUNCHD_TX_OLD_STATE" = active ] && [ "$rollback_rc" -eq 0 ]; then
      mac_launchd_bootstrap_job \
        "$MAC_LAUNCHD_TX_DOMAIN" \
        "${MAC_LAUNCHD_TX_PATHS[0]}" \
        "$MAC_LAUNCHD_TX_TARGET" \
        "$MAC_LAUNCHD_TX_LABEL" \
        "$MAC_LAUNCHD_TX_MODE" || rollback_rc=1
    fi
    if [ -n "$MAC_LAUNCHD_TX_AFTER_RESTORE_HOOK" ] \
      && [ "$rollback_rc" -eq 0 ]; then
      "$MAC_LAUNCHD_TX_AFTER_RESTORE_HOOK" || hook_rc=$?
      [ "$hook_rc" -eq 0 ] || rollback_rc=1
    fi
  fi
  mac_launchd_transaction_cleanup || rollback_rc=1
  mac_launchd_transaction_restore_traps
  if [ "$rollback_rc" -eq 0 ]; then
    printf '%s\n' \
      "${MAC_LAUNCHD_LOG_PREFIX:-[launchd]} restored prior launchd generation: $MAC_LAUNCHD_TX_LABEL" >&2
  else
    mac_launchd_error "could not completely restore prior launchd generation: $MAC_LAUNCHD_TX_LABEL"
  fi
  return "$rollback_rc"
}

mac_launchd_transaction_on_exit() {
  local original_rc="$1" rollback_rc=0 chained_rc=0
  local saved_exit_trap="$MAC_LAUNCHD_TX_SAVED_EXIT_TRAP"
  trap - EXIT HUP INT TERM
  if [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ]; then
    if [ "${MAC_LAUNCHD_TX_RECOVERY_POLICY:-rollback}" = retain-forward ]; then
      printf '%s\n' \
        "${MAC_LAUNCHD_LOG_PREFIX:-[launchd]} retaining failed launchd generation for forward repair: $MAC_LAUNCHD_TX_LABEL" >&2
      mac_launchd_transaction_commit || rollback_rc=$?
    else
      mac_launchd_transaction_rollback || rollback_rc=$?
    fi
    if [ "$original_rc" -eq 0 ] || [ "$rollback_rc" -ne 0 ]; then
      original_rc=1
    fi
  fi
  # Bash does not re-enter a newly restored EXIT trap while it is already
  # processing one. Invoke the saved definition exactly once through the
  # source-scoped RETURN mechanism above, then clear the parent copy.
  trap - EXIT HUP INT TERM
  if [ -n "$saved_exit_trap" ]; then
    mac_launchd_transaction_run_saved_exit_trap \
      "$saved_exit_trap" "$original_rc" \
      || chained_rc=$?
    if [ "$original_rc" -eq 0 ] && [ "$chained_rc" -ne 0 ]; then
      original_rc="$chained_rc"
    fi
  fi
  exit "$original_rc"
}

mac_launchd_transaction_commit() {
  local cleanup_rc=0
  [ "$MAC_LAUNCHD_TX_ACTIVE" -eq 1 ] || {
    mac_launchd_error "cannot commit an inactive launchd transaction"
    return 1
  }
  trap - EXIT
  trap '' HUP INT TERM
  MAC_LAUNCHD_TX_ACTIVE=0
  mac_launchd_transaction_cleanup || cleanup_rc=$?
  mac_launchd_transaction_restore_traps
  if [ "$cleanup_rc" -ne 0 ]; then
    mac_launchd_error \
      "launchd generation committed but transaction cleanup failed: $MAC_LAUNCHD_TX_LABEL"
    return "$cleanup_rc"
  fi
}
