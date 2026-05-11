"""One-off: set 90-day expiration lifecycle on Hetzner Object Storage bucket
for the `recordings/` prefix.

Honours privacy.html §sec-12 + dpa.html §3 commitment: every mirrored call
recording deletes itself 90 days after upload.

Idempotent: re-running just re-applies the same policy.

Usage:
    python scripts/set-hetzner-recording-lifecycle.py [--dry-run]
"""
import os
import sys
from datetime import datetime


def main():
    dry_run = "--dry-run" in sys.argv

    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        print("ERROR: pip install boto3 botocore first", file=sys.stderr)
        sys.exit(1)

    endpoint = os.environ.get("HETZNER_OBJECT_STORAGE_ENDPOINT", "")
    key_id = os.environ.get("HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID", "")
    secret = os.environ.get("HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY", "")
    bucket = os.environ.get("HETZNER_OBJECT_STORAGE_BUCKET", "")

    if not all([endpoint, key_id, secret, bucket]):
        print("ERROR: HETZNER_OBJECT_STORAGE_{ENDPOINT,ACCESS_KEY_ID,SECRET_ACCESS_KEY,BUCKET} env required", file=sys.stderr)
        sys.exit(1)

    if not endpoint.startswith("http"):
        endpoint = "https://" + endpoint

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name="eu-central-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    print(f"Endpoint: {endpoint}")
    print(f"Bucket:   {bucket}")

    # Verify bucket exists + reachable
    try:
        s3.head_bucket(Bucket=bucket)
        print(f"Bucket reachable. OK.")
    except Exception as e:
        print(f"ERROR: head_bucket failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Build lifecycle config:
    #   Rule "callmeie-recordings-90d-expire": every object under recordings/
    #   prefix expires 90 days after creation. Per privacy.html §sec-12.
    lifecycle_config = {
        "Rules": [
            {
                "ID": "callmeie-recordings-90d-expire",
                "Status": "Enabled",
                "Filter": {"Prefix": "recordings/"},
                "Expiration": {"Days": 90},
            }
        ]
    }
    print(f"\nLifecycle policy to apply:")
    print(f"  Rule ID:  callmeie-recordings-90d-expire")
    print(f"  Prefix:   recordings/")
    print(f"  Expires:  Days=90 (auto-delete at day 90)")

    if dry_run:
        print(f"\n[DRY-RUN] not applied")
        return

    # Try to read existing lifecycle for diff
    try:
        existing = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
        print(f"\nExisting lifecycle rules: {len(existing.get('Rules', []))}")
        for r in existing.get("Rules", []):
            print(f"  - {r.get('ID')} Status={r.get('Status')} Filter={r.get('Filter')} Expiration={r.get('Expiration')}")
    except Exception as e:
        if "NoSuchLifecycleConfiguration" in str(e) or "404" in str(e):
            print(f"\nNo existing lifecycle policy on bucket. Applying fresh.")
        else:
            print(f"\nCould not read existing lifecycle ({e}); proceeding.")

    # Apply
    try:
        s3.put_bucket_lifecycle_configuration(
            Bucket=bucket,
            LifecycleConfiguration=lifecycle_config,
        )
        print(f"\nLifecycle policy APPLIED at {datetime.utcnow().isoformat()}Z.")
    except Exception as e:
        print(f"ERROR: put_bucket_lifecycle_configuration failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Verify
    verify = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
    found = any(r.get("ID") == "callmeie-recordings-90d-expire" for r in verify.get("Rules", []))
    print(f"Verification: rule present = {found}")
    if not found:
        sys.exit(1)


if __name__ == "__main__":
    main()
