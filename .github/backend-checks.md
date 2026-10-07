# Backend checks

`backend-offline` runs the complete `scripts/tests` directory except tests marked
`integration`. It runs for every pull request, main push, merge group and manual
request. It has no path filter. The test step receives a clean environment,
private temporary SQLite paths and socket blocking. Unix sockets remain enabled
for local async test plumbing. This is a test boundary, not OS containment.

The workflow uses Python 3.11.16 and hash-pinned action commits. Test dependencies
are resolved from the existing production constraints and `requirements-test.in`
into `requirements-ci.txt`. The Dockerfile still uses its existing requirements.
The CI lock does not change production dependency installation.

Refresh through the existing repository maintenance owner. Review new action,
Python and package versions; regenerate the CI lock with the command in its
header; run the complete offline suite and changed dependency regressions before
accepting new pins. Keep the prior accepted commit available for rollback.

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
