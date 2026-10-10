"""Execute the container entrypoint with stub commands and no database/server."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("sh") is None,
    reason="The production container entrypoint requires POSIX sh",
)]


def invoke(tmp_path, database_url, migration_status):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "commands.jsonl"
    stub = ("#!" + sys.executable + "\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "name = Path(sys.argv[0]).name\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
            "    log.write(json.dumps({'command': name, 'args': sys.argv[1:]}) + '\\n')\n"
            "sys.exit(int(os.environ['MIGRATION_STATUS']) if name == 'alembic' else 0)\n")
    for command in ("alembic", "uvicorn"):
        p = bin_dir / command
        p.write_text(stub)
        p.chmod(0o700)
    env = {"PATH": str(bin_dir) + os.pathsep + os.defpath,
           "COMMAND_LOG": str(calls), "MIGRATION_STATUS": str(migration_status)}
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    script = Path(__file__).resolve().parents[1] / "entrypoint.sh"
    result = subprocess.run([shutil.which("sh"), str(script)], env=env,
                            capture_output=True, text=True, timeout=10)
    records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    return result, records


@pytest.mark.parametrize("status", [23, 127, 143])
def test_failed_migration_stops_before_server(tmp_path, status):
    result, commands = invoke(tmp_path, "postgresql://ci-fixture.invalid/test", status)
    assert result.returncode == status
    assert commands == [{"command": "alembic", "args": ["upgrade", "head"]}]
    assert "aborting server startup" in result.stderr


def test_successful_migration_precedes_server(tmp_path):
    result, commands = invoke(tmp_path, "postgresql://ci-fixture.invalid/test", 0)
    assert result.returncode == 0
    assert [x["command"] for x in commands] == ["alembic", "uvicorn"]
    assert commands[0]["args"] == ["upgrade", "head"]
    assert commands[1]["args"] == ["server:app", "--host", "0.0.0.0", "--port", "8080"]


def test_absent_database_url_preserves_sqlite_startup(tmp_path):
    result, commands = invoke(tmp_path, None, 23)
    assert result.returncode == 0
    assert [x["command"] for x in commands] == ["uvicorn"]
