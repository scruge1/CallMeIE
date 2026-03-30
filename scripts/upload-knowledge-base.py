"""
Upload a knowledge base file to Vapi and attach it to an assistant.

Usage:
    python upload-knowledge-base.py --file products.csv --assistant-id abc123

The AI will then be able to answer questions about the file contents during calls.
Supports: CSV, PDF, TXT, MD, DOC, DOCX
"""

import argparse
import os
import sys
import requests
import json

VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")


def upload_file(filepath: str) -> str:
    """Upload a file to Vapi and return the file ID."""
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        sys.exit(1)

    print(f"Uploading {filepath}...")
    resp = requests.post(
        "https://api.vapi.ai/file",
        headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
        files={"file": open(filepath, "rb")},
    )

    if resp.status_code != 200 and resp.status_code != 201:
        print(f"Upload failed: {resp.status_code} {resp.text}")
        sys.exit(1)

    data = resp.json()
    file_id = data.get("id", data.get("fileId", ""))
    print(f"Uploaded: {file_id}")
    return file_id


def attach_to_assistant(assistant_id: str, file_ids: list):
    """Attach knowledge base files to an assistant."""
    print(f"Attaching {len(file_ids)} files to assistant {assistant_id}...")

    # Get current assistant config
    resp = requests.get(
        f"https://api.vapi.ai/assistant/{assistant_id}",
        headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
    )
    current = resp.json()
    model = current.get("model", {})

    # Add knowledge base
    model["knowledgeBase"] = {"fileIds": file_ids, "provider": "canonical"}

    # Update assistant
    resp = requests.patch(
        f"https://api.vapi.ai/assistant/{assistant_id}",
        headers={
            "Authorization": f"Bearer {VAPI_API_KEY}",
            "Content-Type": "application/json",
        },
        json={"model": model},
    )

    if resp.status_code == 200:
        kb = resp.json().get("model", {}).get("knowledgeBase", {})
        print(f"Knowledge base attached: {len(kb.get('fileIds', []))} files")
    else:
        print(f"Failed: {resp.status_code} {resp.text[:200]}")


def list_files():
    """List all uploaded files."""
    resp = requests.get(
        "https://api.vapi.ai/file",
        headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
    )
    files = resp.json()
    if not files:
        print("No files uploaded yet.")
        return
    print(f"\nUploaded files ({len(files)}):")
    for f in files:
        print(f"  {f.get('id', 'n/a'):40} {f.get('name', f.get('filename', 'unnamed'))}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload knowledge base to Vapi")
    parser.add_argument("--file", help="File to upload (CSV, PDF, TXT, MD)")
    parser.add_argument("--assistant-id", help="Assistant ID to attach the file to")
    parser.add_argument("--list", action="store_true", help="List uploaded files")
    parser.add_argument(
        "--api-key", help="Vapi API key (or set VAPI_API_KEY env var)"
    )
    args = parser.parse_args()

    if args.api_key:
        VAPI_API_KEY = args.api_key

    if not VAPI_API_KEY:
        print("Set VAPI_API_KEY env var or use --api-key")
        sys.exit(1)

    if args.list:
        list_files()
    elif args.file:
        file_id = upload_file(args.file)
        if args.assistant_id:
            attach_to_assistant(args.assistant_id, [file_id])
        else:
            print(f"\nFile ID: {file_id}")
            print("Use --assistant-id to attach it to an assistant")
    else:
        parser.print_help()
