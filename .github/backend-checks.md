# Backend checks

`backend-offline` runs the Python tests in `scripts/tests` except tests marked
`integration`, the existing standalone imported-app fixture, and the
`scripts/tests/*.cjs` source tests on
Node 24.21.0 LTS. It runs for every pull request, main push, merge group and manual
request. It has no path filter. The test step receives a clean environment,
private temporary SQLite paths and socket blocking. Unix sockets remain enabled
for local async test plumbing. This is a test boundary, not OS containment.

The JavaScript tests use local source and VM fixtures, including stub requests.
They need no npm packages or application server. Their clean environment has no
provider credentials; this is not OS network isolation or a live browser test.
The Bash pipeline preserves Node failures through the job shell's `pipefail`.
The retained artifact contains Python JUnit, imported-app fixture output and
JavaScript TAP results. The standalone `test_admin_release_integration.py` is
invoked explicitly: pytest discovery alone does not run its `__main__` path.
It imports the real app with disposable SQLite, synthetic credentials and
provider responses, and guards external application connections. It starts no
listener and does not establish production PostgreSQL or carrier acceptance.

The workflow uses Python 3.11.17 and hash-pinned action commits. The maintained
Dockerfile, runtime inputs and `requirements.lock` remain the backend owner's
release source, as described in `scripts/DEPENDENCY-MAINTENANCE.md`. CI resolves
`requirements.txt` and the retained test pins in `requirements-test.in`,
constrained by that accepted runtime lock. All 61 runtime versions match the CI
lock; six additional test/build packages remain excluded from the runtime.
Before installation, `check_ci_runtime_lock.py` rejects missing or changed runtime
versions, missing runtime distribution hashes, duplicate packages and unsupported
lock syntax. Extra CI-only packages are permitted. This check compares source
locks; it does not inspect a running container or grant release permission.
The former proposed `requirements-runtime.in` and `requirements-runtime.txt`
are marked superseded and are not consumed by the Dockerfile or this workflow.

Regenerate CI with the command in its header after the maintainer accepts a
runtime successor. Verify shared package versions and distribution hashes. The
ARM64 runtime lock and Linux AMD64 CI are different platform observations:
matching pins and passing CI do not establish a new deployed image. Preserve
the runtime owner's actual-image, maintenance and rollback checks.

Refresh through the existing repository maintenance owner. Review new action,
Python and package versions; regenerate the CI lock with the command in its
header; run the complete offline suite and changed dependency regressions before
accepting new pins. Keep the prior accepted commit available for rollback.
Review Node LTS releases and the pinned setup-node action through this same
maintenance owner. Qualify the exact Node binary and all existing CJS tests
before changing their pins. This test runtime does not replace service Node.

Historical local qualification against main da38e2ec on 7 October 2026 ran 149 tests plus
2 subtests with zero failures or skips. The real container shell script is tested
with stub Alembic and Uvicorn commands: migration exit codes 23/127/143 stop
startup, success starts the server after migration, and absent DATABASE_URL
preserves SQLite startup. No real migration or server runs in these tests.

A passing job does not enable branch or deployment enforcement. The repository
owner must select this exact check as required and review administrator/bypass
coverage. The deployment owner must bind production deployment to the accepted
commit/artifact and verify live health and rollback. A main merge can trigger
existing deployment integrations; use their existing release procedure.
