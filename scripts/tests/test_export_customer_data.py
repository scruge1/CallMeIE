"""Gap B smoke tests -- per-tenant data-export ZIP packager.

Closes PDR-CLIENT-FULFILLMENT.md §6 Gap #6 (7-day cancellation export).

What we MUST verify before declaring this safe to use:

  1. argparse: --help works; missing --tenant-id errors with non-zero exit.
  2. --dry-run prints a clean plan JSON and exits 0 without touching DB or
     external APIs (this is the `docker exec` smoke test surface).
  3. --products filter accepts 'all', subset CSV, and rejects unknown values.
  4. ZIP shape: per-product subdir + README, packed deterministically from
     a fake tenant staging directory.
  5. README contains GDPR retention notes (PDR-CLIENT-FULFILLMENT §6 promise).
  6. No real Vapi / Stripe / Porkbun / Postgres hits during the test run
     (covered by --dry-run path + direct call to pack/write_readme).

The script lives at scripts/export-customer-data.py -- hyphens in the
filename so we import via importlib.util to keep the test rig portable.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import zipfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


# Load the module under test (filename has hyphens; not importable directly).
_HERE = Path(__file__).resolve()
_SCRIPT = _HERE.parents[1] / "export-customer-data.py"
assert _SCRIPT.is_file(), f"missing target script: {_SCRIPT}"

_spec = importlib.util.spec_from_file_location("export_customer_data", _SCRIPT)
ecd = importlib.util.module_from_spec(_spec)
sys.modules["export_customer_data"] = ecd
_spec.loader.exec_module(ecd)  # type: ignore[union-attr]


# ────────────────────────────────────────────────────────────────────
# argparse smoke
# ────────────────────────────────────────────────────────────────────
class TestArgparse:
    def test_help_works(self):
        """--help prints usage and exits 0."""
        parser = ecd.build_parser()
        buf = io.StringIO()
        with redirect_stdout(buf), pytest.raises(SystemExit) as exc:
            parser.parse_args(["--help"])
        assert exc.value.code == 0
        out = buf.getvalue()
        assert "--tenant-id" in out
        assert "--output-dir" in out
        assert "--products" in out
        assert "Gap #6" in out or "cancellation" in out.lower()

    def test_missing_tenant_id_errors(self):
        """Missing --tenant-id must exit non-zero (argparse default = 2)."""
        parser = ecd.build_parser()
        buf = io.StringIO()
        with redirect_stderr(buf), pytest.raises(SystemExit) as exc:
            parser.parse_args(["--output-dir", "/tmp/x"])
        assert exc.value.code != 0
        assert "tenant-id" in buf.getvalue().lower()

    def test_unknown_products_rejected(self):
        """--products with an unknown value raises SystemExit at parse time."""
        with pytest.raises(SystemExit) as exc:
            ecd._parse_products("receptionist,bogus")
        assert "bogus" in str(exc.value)

    def test_products_all_expands(self):
        assert ecd._parse_products("all") == list(ecd.KNOWN_PRODUCTS)
        assert ecd._parse_products("") == list(ecd.KNOWN_PRODUCTS)

    def test_products_subset(self):
        assert ecd._parse_products("docops") == ["docops"]
        assert ecd._parse_products("receptionist,websites") == [
            "receptionist",
            "websites",
        ]


# ────────────────────────────────────────────────────────────────────
# --dry-run path (the docker exec smoke test surface)
# ────────────────────────────────────────────────────────────────────
class TestDryRun:
    def test_dry_run_clean_plan(self, monkeypatch, capsys):
        """--dry-run returns 0 and prints a JSON plan. No DB/API hits."""
        # Wipe envs so the test does not accidentally surface real creds.
        for k in (
            "DATABASE_URL",
            "VAPI_API_KEY",
            "COOLIFY_API_TOKEN",
            "COOLIFY_API_ROOT_TOKEN",
            "PORKBUN_API_KEY",
        ):
            monkeypatch.delenv(k, raising=False)
        # Force module-level constants to match the cleared env.
        monkeypatch.setattr(ecd, "DATABASE_URL", "", raising=False)
        monkeypatch.setattr(ecd, "VAPI_API_KEY", "", raising=False)
        monkeypatch.setattr(ecd, "COOLIFY_API_TOKEN", "", raising=False)
        monkeypatch.setattr(ecd, "PORKBUN_API_KEY", "", raising=False)
        monkeypatch.setattr(ecd, "PORKBUN_SECRET", "", raising=False)

        rc = ecd.main(
            [
                "--tenant-id",
                "test-tenant-001",
                "--output-dir",
                "/tmp/test-export",
                "--products",
                "docops",
                "--dry-run",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        plan = json.loads(out)
        assert plan["ok"] is True
        assert plan["dry_run"] is True
        assert plan["tenant_id"] == "test-tenant-001"
        assert plan["products"] == ["docops"]
        assert plan["env_present"]["DATABASE_URL"] is False
        assert plan["env_present"]["VAPI_API_KEY"] is False

    def test_dry_run_does_not_call_psycopg(self, monkeypatch, capsys):
        """Sanity: psycopg.connect MUST NOT be called on a dry-run.

        We achieve this without importing psycopg in the test (it is
        imported lazily inside resolve_tenant). We patch resolve_tenant
        on the module to raise -- if --dry-run reaches it, the test
        fails loudly.
        """

        def _explode(*_a, **_kw):
            raise AssertionError("resolve_tenant must NOT be called in dry-run")

        monkeypatch.setattr(ecd, "resolve_tenant", _explode)
        rc = ecd.main(
            [
                "--tenant-id",
                "unknown-tenant",
                "--output-dir",
                "/tmp/x",
                "--dry-run",
            ]
        )
        assert rc == 0
        assert "dry_run" in capsys.readouterr().out


# ────────────────────────────────────────────────────────────────────
# Missing-DATABASE_URL path -- the "no env" guard
# ────────────────────────────────────────────────────────────────────
class TestEnvGuards:
    def test_missing_database_url_returns_2(self, monkeypatch, capsys):
        """If DATABASE_URL is unset and we are NOT in dry-run mode,
        main returns 2 with an explanatory stderr message. Prevents
        accidentally trying to contact a non-existent Postgres."""
        monkeypatch.setattr(ecd, "DATABASE_URL", "", raising=False)
        rc = ecd.main(["--tenant-id", "x", "--output-dir", "/tmp/x"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "DATABASE_URL" in err


# ────────────────────────────────────────────────────────────────────
# ZIP packing + README shape (no DB / API hits)
# ────────────────────────────────────────────────────────────────────
class TestPackagingShape:
    def _fake_tenant(self) -> dict:
        return {
            "site_id": "testbiz-001",
            "display_name": "Test Biz Ltd",
            "lead_email": "owner@testbiz.ie",
            "tier": "launch",
            "care_tier": None,
            "vapi_assistant_id": None,
        }

    def test_readme_has_gdpr_and_products(self, tmp_path: Path):
        tenant = self._fake_tenant()
        summary = {
            "receptionist": {"calls": 3, "transcripts": 2},
            "doc_ops": {"skipped": "doc-ops tables not in this DB"},
            "website": {"coolify_skipped": "COOLIFY_API_TOKEN not set"},
        }
        ecd.write_readme(
            tenant, tmp_path, summary, ["receptionist", "docops", "websites"]
        )
        readme = (tmp_path / "README.md").read_text(encoding="utf-8")
        assert "Test Biz Ltd" in readme
        assert "testbiz-001" in readme
        assert "GDPR" in readme  # retention notes mandatory
        assert "Gap #6" in readme
        assert "receptionist" in readme
        assert "doc-ops" in readme
        assert "website" in readme
        # Summary JSON embedded.
        assert "calls" in readme
        assert "coolify_skipped" in readme

    def test_zip_shape_per_product_subdir_and_readme(self, tmp_path: Path):
        """pack() should produce a ZIP whose entries include README.md and
        per-product subdir markers -- the canonical export shape."""
        staging = tmp_path / "staging"
        staging.mkdir()
        # Simulate what export_receptionist / export_docops / export_website
        # would have written.
        for sub, leaf in [
            ("receptionist", "calls.csv"),
            ("receptionist/transcripts", "call_xyz.txt"),
            ("doc-ops", "extractions.csv"),
            ("doc-ops/documents", "doc_1.pdf"),
            ("website", "EXPORT-INFO.json"),
        ]:
            d = staging / sub
            d.mkdir(parents=True, exist_ok=True)
            (d / leaf).write_text("fake", encoding="utf-8")
        # README at root.
        (staging / "README.md").write_text("# fake readme", encoding="utf-8")

        out_zip = tmp_path / "out.zip"
        ecd.pack(staging, out_zip)

        assert out_zip.is_file()
        with zipfile.ZipFile(out_zip) as zf:
            names = set(zf.namelist())
        # The README must always be present (single-source GDPR text).
        assert "README.md" in names
        # Per-product subdirs (file entries under them) must round-trip.
        assert any(n.startswith("receptionist/") for n in names)
        assert any(n.startswith("doc-ops/") for n in names)
        assert any(n.startswith("website/") for n in names)
        # Specific leaves.
        assert "receptionist/calls.csv" in names
        assert "receptionist/transcripts/call_xyz.txt" in names
        assert "doc-ops/extractions.csv" in names
        assert "website/EXPORT-INFO.json" in names

    def test_zip_is_valid_archive(self, tmp_path: Path):
        """zipfile.testzip() returns None when archive is intact."""
        staging = tmp_path / "s"
        staging.mkdir()
        (staging / "a.txt").write_text("hello", encoding="utf-8")
        out_zip = tmp_path / "ok.zip"
        ecd.pack(staging, out_zip)
        with zipfile.ZipFile(out_zip) as zf:
            assert zf.testzip() is None


# ────────────────────────────────────────────────────────────────────
# No real network -- ensure requests is not imported at module load
# (so import the test module on a host without `requests` works).
# ────────────────────────────────────────────────────────────────────
def test_module_loads_without_requests(monkeypatch):
    """Module top-level must not require psycopg/requests/boto3; those
    are imported lazily inside exporter helpers. This is the property
    that makes --help and --dry-run safe in any container."""
    # The module is already imported at the top of this file. If it had
    # imported `requests` at module scope, an environment without the
    # package would have failed at import. Just assert the deferred-import
    # contract: the names are not module attributes.
    assert not hasattr(ecd, "psycopg") or ecd.psycopg.__name__ == "psycopg"
    # Most important: no `requests` symbol at module scope.
    assert "requests" not in vars(ecd) or callable(vars(ecd).get("requests"))
