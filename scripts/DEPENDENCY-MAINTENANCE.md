# Backend dependency maintenance

Owner and release path: existing CallMeIE backend/dashboard maintainer, Git
main and Coolify application xml9wji6109b1kergfz05665. Do not add an updater,
listener or parallel deployment service.

requirements.txt contains reviewed direct inputs. requirements.lock is the
Python 3.11 Linux ARM64 resolved package/hash closure. Dockerfile installs that
lock with hash validation and pins the reviewed Python base image. These pins
describe one replaceable generation, not permanent version approval.

Check upstream notices before each release and at least monthly; urgent
security fixes take priority. No automatic update schedule is installed here.
Review FastAPI, Starlette, multipart, Requests and the complete resolved
closure. Available releases are not accepted releases. Record running package
and image metadata in the existing project checkpoint.

To stage a successor, resolve in isolation using the actual Python/platform:

    uv pip compile scripts/requirements.txt --python-version 3.11 --python-platform aarch64-unknown-linux-gnu --generate-hashes --output-file scripts/requirements.lock

Validate package consistency and run tests/test_admin_release_integration.py
against the actual ARM64 candidate with disposable SQLite, mocked providers
and blocked external application connections. It does not establish live
PostgreSQL, carrier, recording, voice quality or all-copy retention acceptance.
Use the existing focused/browser tests when their source or transport changes.
Build the actual Dockerfile before release; Windows tests do not replace this.

Before pushing, confirm the exact predecessor, no unfinished deployment and
the existing bounded active-call check. Use normal fast-forward push only.
Verify actual image/package identity, health, authentication and useful API
behaviour after the existing Coolify deployment finishes.

Retain the accepted predecessor image and Git generation. Existing database,
credential and storage backup/recovery remains with its current owner. An
image rollback does not restore data or undo schema changes. Stop release if
recovery scope or startup migration compatibility is uncertain. Never run
production migrations from this test procedure.

9 October 2026 candidate: FastAPI0.143.0/Starlette1.7.0, multipart0.0.32 and
Requests2.34.2 pass the isolated imported-app fixture on Python3.11.17 ARM64.
Seven installed distributions differ from observed d1e4513 container: those
four plus Google API Core2.42.0, new annotated-doc0.0.5 and
OpenTelemetry API1.45.1. No general vulnerability-free claim is made.
