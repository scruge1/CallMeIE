from pathlib import Path
import tempfile
import unittest

from check_ci_runtime_lock import check_runtime_lock, read_lock


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def pin(self, name="example", version="1.0", digest="a"):
        return f"{name}=={version} \\\n    --hash=sha256:{digest * 64}\n"

    def test_shared_hashes_allow_additional_ci_packages(self):
        runtime = self.write("runtime", self.pin("Example_Package"))
        ci = self.write("ci", self.pin("example-package") + self.pin("test-only"))
        self.assertEqual(check_runtime_lock(runtime, ci), 1)

    def test_missing_or_changed_runtime_pin_is_refused(self):
        runtime = self.write("runtime", self.pin())
        for text in (self.pin("another"), self.pin(version="2.0")):
            with self.subTest(text=text), self.assertRaises(ValueError):
                check_runtime_lock(runtime, self.write("ci", text))

    def test_changed_distribution_hash_is_refused(self):
        runtime = self.write("runtime", self.pin())
        with self.assertRaises(ValueError):
            check_runtime_lock(runtime, self.write("ci", self.pin(digest="b")))

    def test_ambiguous_or_unhashed_input_is_refused(self):
        for text in ("", "example==1.0\n", self.pin() + self.pin(),
                     "-r external.txt\n", "example>=1.0\n", "example==1.0; sys_platform == 'win32'\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                read_lock(self.write("invalid", text))
