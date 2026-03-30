"""
Set up a new client — creates everything needed in one go.

Usage:
    python setup-new-client.py --name "Bright Smile Dental" --config client.json

This script:
1. Clones the assistant template with client-specific details
2. Creates Google Calendar tools (availability check + event create)
3. Creates transfer call tool with client's emergency number
4. Creates SMS tool
5. Uploads knowledge base file if provided
6. Creates query tool for product/inventory search
7. Assigns all tools to the new assistant
8. Outputs the assistant ID and instructions for next steps

The client.json file should contain:
{
    "business_name": "Bright Smile Dental",
    "ai_name": "Sarah",
    "address": "123 O'Connell Street, Limerick",
    "hours": "Monday to Friday 9am to 5:30pm. Saturday 9am to 1pm. Closed Sunday.",
    "services": "Check-ups, cleanings, fillings, crowns, root canals, emergency care.",
    "insurance": "All major dental insurance, medical cards, PRSI, self-pay.",
    "parking": "Free parking behind the building.",
    "emergency_number": "+353851234567",
    "receptionist_number": "+353611234567",
    "timezone": "Europe/Dublin",
    "knowledge_base_file": "products.txt",
    "industry": "dental"
}
"""

import argparse
import json
import os
import sys
import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
TEMPLATE_PROMPT = """You are {ai_name}, the receptionist at {business_name} in Limerick. You answer phone calls, help callers with questions, and book appointments.

VOICE RULES - these override everything:
- All responses must be plain text. No markdown, no bullet points, no numbered lists.
- Keep every response to 1-2 sentences max. Never monologue.
- Ask ONE question at a time. Never combine questions.
- Use contractions naturally.
- Dates: say next Tuesday at 2 or this Saturday morning. Never use date formats.
- Times: say 2 PM or half ten in the morning.
- After the caller answers, acknowledge what they said before asking the next thing.
- Sound like a real Irish receptionist. Use phrases like Grand, Lovely, No bother, Sure thing, Perfect.

PHONE NUMBER READBACK:
This is Ireland. No country codes. No plus one.
Write phone numbers as spoken words: zero eight five, one two three, seven eight nine.
NEVER write as digits. If confusing, ask caller to repeat slowly.

BOOKING FLOW - one piece at a time:
1. What they need
2. Preferred day and time. Suggest options if vague.
3. Check availability using calendar tool.
4. If unavailable, offer alternatives.
5. Name (confirm spelling).
6. Phone number (read back as words).
7. Insurance: dental insurance, medical card, PRSI, or self-pay.
8. Book using calendar tool.
9. Send confirmation text.
10. Close warmly.

CANCELLATIONS: Take name and appointment date. Say the team will sort it out.
EMERGENCIES: Severe pain, bleeding, broken tooth, swelling → transfer immediately.
PRODUCT QUESTIONS: Search the knowledge base for stock, prices, availability.

WHAT NOT TO DO: Never dump multiple questions. Never use date formats. Never quote prices unless knowledge base has them. Never give medical advice.

BUSINESS INFO:
Hours: {hours}
Address: {address}
Services: {services}
Insurance: {insurance}
Parking: {parking}
This call may be recorded for quality and training purposes."""


def create_client(config: dict) -> dict:
    """Create full client setup on Vapi."""
    headers = {
        "Authorization": f"Bearer {VAPI_API_KEY}",
        "Content-Type": "application/json",
    }
    results = {}

    name = config["business_name"]
    print(f"\n{'='*50}")
    print(f"  Setting up: {name}")
    print(f"{'='*50}\n")

    # 1. Create assistant
    prompt = TEMPLATE_PROMPT.format(**config)
    print("[1/6] Creating assistant...")
    resp = requests.post(
        "https://api.vapi.ai/assistant",
        headers=headers,
        json={
            "name": f"{name} Receptionist",
            "firstMessage": f"Hi, thanks for ringing {name}! This is {config['ai_name']}. How can I help you today?",
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "maxTokens": 250,
                "temperature": 0.7,
                "messages": [{"role": "system", "content": prompt}],
            },
            "voice": {"provider": "11labs", "voiceId": "dN8hviqdNrAsEcL57yFj"},
            "transcriber": {
                "provider": "deepgram",
                "model": "nova-3",
                "language": "en",
                "smartFormat": True,
                "numerals": True,
            },
        },
    )
    assistant = resp.json()
    assistant_id = assistant.get("id", "")
    results["assistant_id"] = assistant_id
    print(f"    Assistant: {assistant_id}")

    # 2. Create calendar tools
    print("[2/6] Creating calendar tools...")
    tz = config.get("timezone", "Europe/Dublin")

    # Check availability
    resp = requests.post(
        "https://api.vapi.ai/tool",
        headers=headers,
        json={
            "type": "google.calendar.availability.check",
            "metadata": {"calendarId": "primary", "timeZone": tz},
        },
    )
    check_tool = resp.json().get("id", "")

    # Create event
    resp = requests.post(
        "https://api.vapi.ai/tool",
        headers=headers,
        json={
            "type": "google.calendar.event.create",
            "metadata": {"calendarId": "primary", "timeZone": tz},
        },
    )
    create_tool = resp.json().get("id", "")
    print(f"    Calendar check: {check_tool}")
    print(f"    Calendar create: {create_tool}")

    # 3. Create transfer tool
    print("[3/6] Creating transfer tool...")
    emergency = config.get("emergency_number", "")
    resp = requests.post(
        "https://api.vapi.ai/tool",
        headers=headers,
        json={
            "type": "transferCall",
            "destinations": [
                {
                    "type": "number",
                    "number": emergency,
                    "message": "Transferring you now.",
                    "description": "Emergency on-call",
                }
            ],
        },
    )
    transfer_tool = resp.json().get("id", "")
    print(f"    Transfer: {transfer_tool}")

    # 4. Create SMS tool
    print("[4/6] Creating SMS tool...")
    resp = requests.post(
        "https://api.vapi.ai/tool", headers=headers, json={"type": "sms"}
    )
    sms_tool = resp.json().get("id", "")
    print(f"    SMS: {sms_tool}")

    # 5. Upload knowledge base (if provided)
    kb_tool = ""
    kb_file = config.get("knowledge_base_file", "")
    if kb_file and os.path.exists(kb_file):
        print(f"[5/6] Uploading knowledge base: {kb_file}")
        resp = requests.post(
            "https://api.vapi.ai/file",
            headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
            files={"file": (os.path.basename(kb_file), open(kb_file, "rb"), "text/plain")},
        )
        file_id = resp.json().get("id", "")
        if file_id:
            resp = requests.post(
                "https://api.vapi.ai/tool",
                headers=headers,
                json={
                    "type": "query",
                    "function": {"name": "search_products"},
                    "knowledgeBases": [
                        {
                            "provider": "google",
                            "name": f"{name}-inventory",
                            "description": "Product inventory, services, pricing, and business information.",
                            "fileIds": [file_id],
                        }
                    ],
                },
            )
            kb_tool = resp.json().get("id", "")
            print(f"    Knowledge base: {kb_tool}")
    else:
        print("[5/6] No knowledge base file — skipping")

    # 6. Assign all tools to assistant
    print("[6/6] Assigning tools to assistant...")
    tool_ids = [t for t in [check_tool, create_tool, transfer_tool, sms_tool, kb_tool] if t]

    resp = requests.patch(
        f"https://api.vapi.ai/assistant/{assistant_id}",
        headers=headers,
        json={
            "model": {
                "provider": "anthropic",
                "model": "claude-haiku-4-5-20251001",
                "maxTokens": 250,
                "temperature": 0.7,
                "messages": [{"role": "system", "content": prompt}],
                "toolIds": tool_ids,
                "tools": [],
            }
        },
    )
    final_tools = resp.json().get("model", {}).get("toolIds", [])
    print(f"    {len(final_tools)} tools assigned")

    results["tools"] = {
        "calendar_check": check_tool,
        "calendar_create": create_tool,
        "transfer": transfer_tool,
        "sms": sms_tool,
        "knowledge_base": kb_tool,
    }

    print(f"\n{'='*50}")
    print(f"  SETUP COMPLETE: {name}")
    print(f"  Assistant ID: {assistant_id}")
    print(f"  Tools: {len(final_tools)}")
    print(f"{'='*50}")
    print(f"\nNEXT STEPS:")
    print(f"  1. Buy +353 phone number in Vapi and assign to this assistant")
    print(f"  2. Connect Google Calendar for this client")
    print(f"  3. Add bank holidays and lunch breaks to calendar")
    print(f"  4. Test 10+ call scenarios")
    print(f"  5. Set up call forwarding on client's phone")

    # Save results
    output = f"client-setup-{name.lower().replace(' ', '-')}.json"
    with open(output, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nConfig saved to: {output}")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Set up new client")
    parser.add_argument("--config", required=True, help="Client config JSON file")
    parser.add_argument("--api-key", help="Vapi API key")
    args = parser.parse_args()

    if args.api_key:
        VAPI_API_KEY = args.api_key
    if not VAPI_API_KEY:
        print("Set VAPI_API_KEY or use --api-key")
        sys.exit(1)

    with open(args.config) as f:
        config = json.load(f)

    create_client(config)
