"""Darwin hub upgrade staging must find Homebrew Postgres without PATH."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start-test-postgres.sh"


def test_helper_discovers_homebrew_postgres_without_relying_on_path():
    """Live 2026-08-27: hub upgrade staging failed because launchd PATH had
    no pg_isready even though Homebrew postgresql@17 was listening."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "/opt/homebrew/opt/postgresql@17/bin" in source
    assert 'PATH="$brew_bin:${PATH:-}"' in source
