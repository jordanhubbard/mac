from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class BeadsCommandResult:
    argv: List[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stderr or self.stdout or "").strip()


class BeadsBridgeService:
    """Narrow boundary around the external bd CLI.

    ControlPlane still owns task/import policy, but subprocess execution is
    injectable here so Beads sync behavior can be tested without shelling out.
    """

    def __init__(
        self,
        cli_path: Callable[[], str],
        *,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        self._cli_path = cli_path
        self._runner = runner

    def cli_path(self) -> str:
        return self._cli_path()

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        actor: Optional[str] = None,
        timeout: int = 20,
    ) -> BeadsCommandResult:
        argv = [self.cli_path()]
        if actor:
            argv.extend(["--actor", actor])
        argv.extend(str(item) for item in args)
        try:
            completed = self._runner(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            if not stderr:
                stderr = "timed out after %ss" % timeout
            return BeadsCommandResult(
                argv=list(argv),
                cwd=str(cwd),
                returncode=124,
                stdout=stdout,
                stderr=stderr,
            )
        return BeadsCommandResult(
            argv=list(argv),
            cwd=str(cwd),
            returncode=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )
