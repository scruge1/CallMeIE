"""Smoke tests for voice_catalog. Run with: python -m unittest test_voice_catalog -v

Mirror of the same file in callmeie-hub/receptionist/scripts/. Keep in lockstep.
"""
from __future__ import annotations
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_catalog as vc  # noqa: E402


class VoiceCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        vc.refresh()

    def test_default_with_no_args(self) -> None:
        entry = vc.voice_for_industry()
        self.assertEqual(entry["voice_id"], "3Q0HiHNecynsdqicntLT")

    def test_canonical_industry_keys(self) -> None:
        self.assertEqual(vc.voice_for_industry(industry="dental")["voice_id"], "kOvUpYLYS0rKGldsKcD1")
        self.assertEqual(vc.voice_for_industry(industry="solicitor")["voice_id"], "U3AWuAe8WcVA50PuDMrY")

    def test_business_type_freeform(self) -> None:
        self.assertEqual(vc.voice_for_industry(business_type="Dental Practice")["voice_id"], "kOvUpYLYS0rKGldsKcD1")
        self.assertEqual(vc.voice_for_industry(business_type="Garage")["voice_id"], "LhG6Tsjmn5tklSCyReiu")
        self.assertEqual(vc.voice_for_industry(business_type="Hair & Beauty")["voice_id"], "3b8fXc91YHS1i2DYAlBQ")

    def test_unknown_business_type_falls_through_to_default(self) -> None:
        self.assertEqual(vc.voice_for_industry(business_type="Unknown Trade")["voice_id"], "3Q0HiHNecynsdqicntLT")

    def test_missing_catalog_returns_legacy_fallback(self) -> None:
        os.environ["CALLMEIE_VOICE_CATALOG"] = "/nonexistent/path.json"
        vc.refresh()
        try:
            entry = vc.voice_for_industry(industry="dental")
            self.assertEqual(entry["voice_id"], "dN8hviqdNrAsEcL57yFj")
        finally:
            del os.environ["CALLMEIE_VOICE_CATALOG"]
            vc.refresh()


if __name__ == "__main__":
    unittest.main(verbosity=2)
