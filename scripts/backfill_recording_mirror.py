"""Backfill Vapi → Hetzner recording mirror for one call_id.

Use when /vapi/call-ended fired before Vapi recording URLs were populated
(the race condition documented in 2026-05-20 diagnosis). Idempotent: skips
upload if the Hetzner key already exists.

Usage (PowerShell):
    $env:VAPI_API_KEY = "..."
    $env:HETZNER_OBJECT_STORAGE_ENDPOINT = "..."
    $env:HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID = "..."
    $env:HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY = "..."
    $env:HETZNER_OBJECT_STORAGE_BUCKET = "callmeie-corpus"
    python backfill_recording_mirror.py <call_id> [<call_id> ...]

Or with --env-file pointing at an existing .env file.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
from botocore.client import Config


def load_env_file(path: Path) -> None:
    """Minimal .env loader — only populates vars we need + doesn't overwrite."""
    if not path.exists():
        return
    wanted = {
        "VAPI_API_KEY",
        "HETZNER_OBJECT_STORAGE_ENDPOINT",
        "HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID",
        "HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY",
        "HETZNER_OBJECT_STORAGE_BUCKET",
    }
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in wanted and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


def fetch_vapi_call(call_id: str, api_key: str) -> dict:
    r = requests.get(
        f"https://api.vapi.ai/call/{call_id}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def extract_recording_urls(call: dict) -> tuple[str, str, str]:
    """Return (mono_url, stereo_url, assistant_id)."""
    artifact = call.get("artifact") or {}
    mono = (
        artifact.get("recordingUrl")
        or call.get("recordingUrl")
        or ""
    )
    stereo = (
        artifact.get("stereoRecordingUrl")
        or call.get("stereoRecordingUrl")
        or ""
    )
    # Fallback to artifact.recording.* nesting (Vapi schema variant)
    rec = (artifact.get("recording") or {})
    if not stereo:
        stereo = rec.get("stereoUrl") or ""
    if not mono:
        mono = (rec.get("mono") or {}).get("combinedUrl") or ""
    assistant_id = (
        call.get("assistantId")
        or (call.get("assistant") or {}).get("id")
        or ""
    )
    return mono, stereo, assistant_id


def hetzner_s3():
    ep = os.environ["HETZNER_OBJECT_STORAGE_ENDPOINT"]
    ak = os.environ["HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID"]
    sk = os.environ["HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY"]
    return boto3.client(
        "s3",
        aws_access_key_id=ak,
        aws_secret_access_key=sk,
        endpoint_url=ep,
        region_name="eu-central",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def key_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def mirror_one(call_id: str, api_key: str, bucket: str) -> dict:
    call = fetch_vapi_call(call_id, api_key)
    mono_url, stereo_url, assistant_id = extract_recording_urls(call)
    if not (mono_url or stereo_url):
        return {"call_id": call_id, "status": "no_recording_on_vapi"}

    s3 = hetzner_s3()
    result = {"call_id": call_id, "assistant_id": assistant_id, "channels": {}}

    for label, url in [("mono", mono_url), ("stereo", stereo_url)]:
        if not url:
            continue
        key = f"recordings/{call_id}{'' if label == 'mono' else '.stereo'}.wav"
        if key_exists(s3, bucket, key):
            result["channels"][label] = {"key": key, "status": "already_exists"}
            continue
        resp = requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        content = resp.content
        ctype = resp.headers.get("content-type", "audio/wav")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType=ctype,
            Metadata={
                "vapi-call-id": call_id,
                "vapi-assistant-id": assistant_id,
                "channel": label,
                "mirrored-at": datetime.now(timezone.utc).isoformat(),
                "backfilled": "true",
            },
        )
        result["channels"][label] = {"key": key, "status": "uploaded", "size": len(content)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("call_ids", nargs="+")
    parser.add_argument("--env-file", default="")
    args = parser.parse_args()

    if args.env_file:
        load_env_file(Path(args.env_file))

    api_key = os.environ.get("VAPI_API_KEY", "").strip()
    bucket = os.environ.get("HETZNER_OBJECT_STORAGE_BUCKET", "").strip()
    missing = [
        k
        for k in [
            "VAPI_API_KEY",
            "HETZNER_OBJECT_STORAGE_ENDPOINT",
            "HETZNER_OBJECT_STORAGE_ACCESS_KEY_ID",
            "HETZNER_OBJECT_STORAGE_SECRET_ACCESS_KEY",
            "HETZNER_OBJECT_STORAGE_BUCKET",
        ]
        if not os.environ.get(k)
    ]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    rc = 0
    for cid in args.call_ids:
        try:
            res = mirror_one(cid, api_key, bucket)
            print(res)
        except Exception as e:
            print({"call_id": cid, "status": "error", "error": str(e)})
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
