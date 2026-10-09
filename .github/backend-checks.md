# Backend checks

`backend-offline` runs the Python tests in `scripts/tests` except tests marked
`integration`, followed by the existing `scripts/tests/*.cjs` source tests on
Node 24.21.0 LTS. It runs for every pull request, main push, merge group and manual
request. It has no path filter. The test step receives a clean environment,
private temporary SQLite paths and socket blocking. Unix sockets remain enabled
for local async test plumbing. This is a test boundary, not OS containment.

The JavaScript tests use local source and VM fixtures, including stub requests.
They need no npm packages or application server. Their clean environment has no
provider credentials; this is not OS network isolation or a live browser test.
The Bash pipeline preserves Node failures through the job shell's `pipefail`.
The retained artifact contains Python JUnit and JavaScript TAP results.

The workflow uses Python 3.11.16 and hash-pinned action commits. Test dependencies
are resolved from the existing production constraints and `requirements-test.in`
into `requirements-ci.txt`. The Dockerfile pins the official Python 3.11.16
multi-architecture image digest and installs `requirements-runtime.txt` with
hash checking. Compile that runtime lock from `requirements-runtime.in`,
constrained by the CI lock. Its 60 packages match CI; the five test-only packages
are excluded. The explicit packaging pin keeps shared build metadata aligned.

Regenerate CI first, then the runtime lock, using the portable commands in their
headers. Review upstream releases and selectively update packages; preserve
accepted versions for other packages. Verify every runtime pin and distribution
hash belongs to the CI lock. Check the actual image interpreter, installed
packages, shipped source and application behavior before accepting a release.
Base-image updates need fresh official digest lookup and the same checks.

The Requests 2.32.0 pin was withdrawn upstream. This candidate selects the
current stable 2.34.2 release; acceptance requires the updated offline checks.

Refresh through the existing repository maintenance owner. Review new action,
Python and package versions; regenerate the CI lock with the command in its
header; run the complete offline suite and changed dependency regressions before
accepting new pins. Keep the prior accepted commit available for rollback.
Review Node LTS releases and the pinned setup-node action through this same
maintenance owner. Qualify the exact Node binary and all existing CJS tests
before changing their pins. This test runtime does not replace service Node.

Local qualification against main da38e2ec on 7 October 2026 ran 149 tests plus
2 subtests with zero failures or skips. The real container shell script is tested
with stub Alembic and Uvicorn commands: migration exit codes 23/127/143 stop
startup, success starts the server after migration, and absent DATABASE_URL
preserves SQLite startup. No real migration or server runs in these tests.

A passing job does not enable branch or deployment enforcement. The repository
owner must select this exact check as required and review administrator/bypass
coverage. The deployment owner must bind production deployment to the accepted
commit/artifact and verify live health and rollback. A main merge can trigger
existing deployment integrations; use their existing release procedure.
