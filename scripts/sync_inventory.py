"""
Sync a client's Google Sheet inventory to Vapi knowledge base.

This runs on a schedule (every 5 minutes via Make.com or cron).
When the business updates their Google Sheet (adds products, changes stock,
updates prices), this script exports the sheet as CSV, uploads to Vapi,
and updates the assistant's knowledge base.

The result: business edits their sheet → AI knows about it within 5 minutes.

Usage:
    python sync-inventory.py --sheet-id GOOGLE_SHEET_ID --assistant-id VAPI_ASSISTANT_ID

Environment:
    VAPI_API_KEY — Vapi private API key
    GOOGLE_SHEETS_API_KEY — Google Sheets API key (for public sheets)
    OR use service account JSON for private sheets
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
LAST_HASH_FILE = os.path.expanduser("~/.ai-agency-inventory-hashes.json")


def get_sheet_as_csv(sheet_id: str, sheet_name: str = "Sheet1") -> str:
    """Download a Google Sheet as CSV via the public export URL."""
    # This works for sheets shared as "anyone with the link can view"
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/gviz/tq?tqx=out:csv&sheet={sheet_name}"
    resp = requests.get(url)
    if resp.status_code != 200:
        print(f"Failed to fetch sheet: {resp.status_code}")
        return ""
    return resp.text


def content_hash(content: str) -> str:
    """SHA256 hash of content to detect changes."""
    return hashlib.sha256(content.encode()).hexdigest()


def load_last_hashes() -> dict:
    """Load previously synced content hashes."""
    if os.path.exists(LAST_HASH_FILE):
        with open(LAST_HASH_FILE) as f:
            return json.load(f)
    return {}


def save_hash(sheet_id: str, hash_val: str):
    """Save the current hash to avoid unnecessary re-uploads."""
    hashes = load_last_hashes()
    hashes[sheet_id] = hash_val
    with open(LAST_HASH_FILE, "w") as f:
        json.dump(hashes, f)


def upload_to_vapi(csv_content: str, filename: str) -> str:
    """Upload CSV content to Vapi and return file ID."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".csv", delete=False, encoding="utf-8"
    ) as f:
        f.write(csv_content)
        temp_path = f.name

    try:
        resp = requests.post(
            "https://api.vapi.ai/file",
            headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
            files={"file": (filename, open(temp_path, "rb"), "text/csv")},
        )
        if resp.status_code in (200, 201):
            file_id = resp.json().get("id", "")
            print(f"Uploaded: {file_id}")
            return file_id
        else:
            print(f"Upload failed: {resp.status_code} {resp.text[:200]}")
            return ""
    finally:
        os.unlink(temp_path)


def update_assistant_kb(assistant_id: str, file_id: str, kb_name: str):
    """Update the assistant's knowledge base query tool with new file."""
    # Get current tools
    resp = requests.get(
        f"https://api.vapi.ai/assistant/{assistant_id}",
        headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
    )
    assistant = resp.json()

    # Find the query tool and update its fileIds
    tool_ids = assistant.get("model", {}).get("toolIds", [])

    for tool_id in tool_ids:
        tool_resp = requests.get(
            f"https://api.vapi.ai/tool/{tool_id}",
            headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
        )
        tool = tool_resp.json()
        if tool.get("type") == "query":
            # Update the knowledge base fileIds
            kbs = tool.get("knowledgeBases", [])
            for kb in kbs:
                kb["fileIds"] = [file_id]

            requests.patch(
                f"https://api.vapi.ai/tool/{tool_id}",
                headers={
                    "Authorization": f"Bearer {VAPI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={"knowledgeBases": kbs},
            )
            print(f"Updated query tool {tool_id} with new file")
            return

    print("No query tool found on assistant. Create one first.")


def sync(sheet_id: str, assistant_id: str, sheet_name: str = "Sheet1"):
    """Full sync: check for changes, upload if needed, update assistant."""
    print(f"Checking sheet {sheet_id}...")

    csv_content = get_sheet_as_csv(sheet_id, sheet_name)
    if not csv_content:
        print("No data from sheet")
        return

    # Check if content changed
    current_hash = content_hash(csv_content)
    last_hashes = load_last_hashes()

    if last_hashes.get(sheet_id) == current_hash:
        print("No changes detected. Skipping upload.")
        return

    print(f"Changes detected! Uploading...")
    lines = csv_content.strip().split("\n")
    print(f"  Rows: {len(lines) - 1} (excluding header)")

    file_id = upload_to_vapi(csv_content, f"inventory_{sheet_id[:8]}.csv")
    if not file_id:
        return

    update_assistant_kb(assistant_id, file_id, "product-inventory")
    save_hash(sheet_id, current_hash)
    print("Sync complete!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Google Sheet to Vapi KB")
    parser.add_argument("--sheet-id", required=True, help="Google Sheet ID")
    parser.add_argument("--assistant-id", required=True, help="Vapi Assistant ID")
    parser.add_argument("--sheet-name", default="Sheet1", help="Sheet tab name")
    parser.add_argument("--api-key", help="Vapi API key")
    args = parser.parse_args()

    if args.api_key:
        VAPI_API_KEY = args.api_key

    if not VAPI_API_KEY:
        print("Set VAPI_API_KEY or use --api-key")
        sys.exit(1)

    sync(args.sheet_id, args.assistant_id, args.sheet_name)
