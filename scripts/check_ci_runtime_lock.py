"""Check shared runtime pins and hashes before the existing CI install."""

from pathlib import Path
import re


def read_lock(path):
    packages = {}
    current = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        pin = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;\\]+)\s*\\?", stripped)
        digest = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})\s*\\?", stripped)
        if pin:
            name = re.sub(r"[-_.]+", "-", pin[1]).lower()
            if name in packages:
                raise ValueError(f"Duplicate package in {path}: {name}")
            current = {"version": pin[2], "hashes": set()}
            packages[name] = current
        elif digest and current is not None:
            current["hashes"].add(digest[1])
        else:
            raise ValueError(f"Unsupported lock syntax in {path}")
    if not packages or any(not p["hashes"] for p in packages.values()):
        raise ValueError(f"Empty or unhashed lock: {path}")
    return packages


def check_runtime_lock(runtime_path, ci_path):
    runtime, ci = read_lock(runtime_path), read_lock(ci_path)
    for name, package in runtime.items():
        candidate = ci.get(name)
        if candidate is None or candidate["version"] != package["version"]:
            raise ValueError(f"Runtime/CI version mismatch: {name}")
        if not package["hashes"].issubset(candidate["hashes"]):
            raise ValueError(f"Runtime/CI distribution hash mismatch: {name}")
    return len(runtime)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    count = check_runtime_lock(root / "requirements.lock", root / "requirements-ci.txt")
    print(f"Runtime/CI lock check passed: {count} shared packages")
