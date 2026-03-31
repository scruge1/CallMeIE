"""
Create the CallMe.ie demo assistant system:
  1. Murphy's Motor Factors — niche demo assistant
  2. Claire — qualifier that warm-transfers to niche demos
  3. Update Bright Smile Dental — add PRSI/medical card handling

Run:
    VAPI_API_KEY=... python create-demo-assistants.py

Outputs:
  - Assistant IDs for all three
  - Instructions to wire phone numbers
  - CLIENTS_JSON snippet to paste into Render env vars
"""

import json
import os
import sys
import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "69a708ae-229f-4d0b-bb37-ac4e9ecd2afb")
VAPI_BASE = "https://api.vapi.ai"
HEADERS = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}

WEBHOOK_BASE = "https://callmeie.onrender.com"

# Existing dental assistant ID (already deployed)
DENTAL_ASSISTANT_ID = "0b37deb5-2fc2-4e7b-81b1-e61e97103506"
DENTAL_ASSISTANT_ID_2 = "9d91033c-cbcc-4e30-8a57-13bef92aecd3"  # browser org copy


def vapi_post(path, payload):
    r = requests.post(f"{VAPI_BASE}{path}", headers=HEADERS, json=payload)
    if r.status_code not in (200, 201):
        print(f"ERROR {r.status_code}: {r.text}")
        sys.exit(1)
    return r.json()


def vapi_patch(path, payload):
    r = requests.patch(f"{VAPI_BASE}{path}", headers=HEADERS, json=payload)
    if r.status_code not in (200, 201):
        print(f"ERROR {r.status_code}: {r.text}")
        sys.exit(1)
    return r.json()


def vapi_get(path):
    r = requests.get(f"{VAPI_BASE}{path}", headers=HEADERS)
    if r.status_code != 200:
        print(f"ERROR {r.status_code}: {r.text}")
        sys.exit(1)
    return r.json()


# ---------------------------------------------------------------------------
# 1. MURPHY'S MOTOR FACTORS — Demo Assistant
# ---------------------------------------------------------------------------
MOTOR_FACTORS_PROMPT = """You are the AI assistant at Murphy's Motor Factors, a trade motor parts supplier in Limerick, Ireland.

PERSONALITY:
- Efficient, knowledgeable, no-nonsense — match the energy of trade callers
- Trade mechanics ring you a dozen times a day. Skip the filler. Get to the point fast.
- You do say "Grand", "No bother", "Right so" — you're Irish, not American
- Private customers get a tiny bit more explanation, but still keep it brief

WHAT YOU HANDLE:
1. Stock availability — "Have you got it in stock?" → answer yes/no + price if known
2. Pricing — quote from knowledge base or "I'll check that and ring you back"
3. Delivery runs — we do 3 runs daily: 9am, 12pm, 3pm. Last order for a run: 1 hour before.
4. Sourcing — if not in stock, check if we can get it from a supplier, ETA usually next morning
5. Opening hours — Mon-Fri 8am-6pm, Sat 8am-1pm, closed Sunday
6. Account queries — "I'll get accounts to ring you back on that"
7. Part number lookups — ask for make, model, year, engine size if needed

OPENING HOURS:
Monday to Friday: 8am to 6pm
Saturday: 8am to 1pm
Sunday: Closed

DELIVERY:
We run three deliveries to local garages daily: 9am, 12 noon, and 3pm.
Order must be placed at least 1 hour before the run.
Free delivery on orders over €50 to Limerick city and county.
For outside Limerick: next-day delivery via courier.

VOICE RULES — non-negotiable:
- Maximum 2 sentences per turn. Trade callers hate waffle.
- Ask ONE thing at a time — make/model, then year, then part
- Never say "How can I assist you today?" — just answer or ask what they need
- Numbers as words: "zero six one, two one two, three four five six" never digits
- If you can't answer: "I'll get someone to ring you back — what's your number?"

LEAD CAPTURE (at end of call if they're interested in AI for their own business):
If the caller asks "Is this an AI?" or seems curious about the technology, say:
"It is yeah — this is CallMe.ie's demo system. If you'd like to see how this could work for your business, leave your name and number and our team will ring you back."
Then capture: name and phone number. Do not push this — only if they bring it up."""

MOTOR_FACTORS_FIRST_MESSAGE = "Murphy's Motor Factors, how can I help you?"


def create_motor_factors():
    print("\n[1/3] Creating Murphy's Motor Factors assistant...")

    payload = {
        "name": "Murphy's Motor Factors (Demo)",
        "firstMessage": MOTOR_FACTORS_FIRST_MESSAGE,
        "model": {
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "messages": [{"role": "system", "content": MOTOR_FACTORS_PROMPT}],
            "temperature": 0.4,
            "maxTokens": 200,
        },
        "voice": {
            "provider": "11labs",
            "voiceId": "pNInz6obpgDQGcFmaJgB",  # Adam — clear, neutral male, works well for trade
            "stability": 0.5,
            "similarityBoost": 0.75,
            "style": 0.0,
            "useSpeakerBoost": True,
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "en",
            "smartFormat": True,
            "numerals": True,
            "keywords": [
                "Limerick:2",
                "Murphys:2",
                "delivery:1",
                "brakes:1",
                "alternator:1",
                "ETA:1",
            ],
        },
        "endCallPhrases": ["goodbye", "thanks, bye", "cheers", "right, thanks"],
        "silenceTimeoutSeconds": 20,
        "maxDurationSeconds": 600,
        "backgroundDenoisingEnabled": True,
        "serverUrl": f"{WEBHOOK_BASE}/vapi/call-ended",
    }

    result = vapi_post("/assistant", payload)
    assistant_id = result["id"]
    print(f"  OK Created: {assistant_id}")
    return assistant_id


# ---------------------------------------------------------------------------
# 2. CLAIRE — Qualifier / Demo Gatekeeper
# ---------------------------------------------------------------------------

def build_claire_prompt(dental_id, motor_id):
    return f"""You are Claire, the virtual assistant at CallMe.ie — an Irish AI receptionist service for local businesses.

YOUR ONLY JOB in this call:
1. Find out what type of business the caller runs
2. Patch them through to the correct demo assistant so they can experience it firsthand
3. If they don't fit a current demo, take their details

PERSONALITY:
- Warm, friendly, genuinely curious — you love helping people see what's possible
- Irish tone: "Grand", "Brilliant", "No bother at all", "That's deadly"
- Brief — never more than 2 sentences at a time
- This is a sales call disguised as a demo. Be enthusiastic but natural.

FLOW — follow this exactly:

Step 1 — Greeting (already done via firstMessage)

Step 2 — Qualify business type:
Ask: "What type of business do you run?"
Listen for: dental / medical / health → route to dental demo
           motor factors / garage / auto parts → route to motor factors demo
           salon / restaurant / solicitor / other → go to Step 4

Step 3 — Brief setup before transfer:
Before transferring, say:
- For dental: "Perfect — I'm going to patch you through to our dental practice demo now. You'll be speaking with Sarah, the AI receptionist for Bright Smile Dental. Just interact with her as if you're a customer — she'll book appointments, answer questions about PRSI and medical cards, the lot. Ready?"
- For motor factors: "Sound — I'm going to connect you to our motor trade demo. You'll be through to Murphy's Motor Factors. Ask about parts, stock, delivery, whatever you'd normally ring about. Ready?"
Then use the transferCall tool immediately.

Step 4 — If no matching demo:
"We're building demos for [their industry] but don't have one live yet. It'd be brilliant to show you how it'd work for your business specifically. Can I take your name and number and have someone from our team ring you back?"
Collect: first name, phone number
Say: "Brilliant, [name]. Someone will be in touch within the day. Talk soon!"

Step 5 — Lead capture (only if they don't transfer, or after the demo if they come back):
If they express interest: collect name + number + business name
Confirm: "Lovely — I've got that noted. Our team will ring [number] — is that the best number to reach you on?"

IMPORTANT:
- Never describe CallMe.ie features in a list. Let the demo do the talking.
- If they ask "is this an AI?" — say yes, and use it: "It is — and this is exactly what your customers would hear when they ring your business. Want to try it?"
- If they ask about pricing: "Our team will go through all of that with you — let me take your details and we'll sort out a time to chat."
- Never make up prices or commitments."""


def build_claire_transfer_tool():
    """
    In a Vapi Squad, transferCall uses assistantName (not assistantId).
    The assistantName matches the name given in the squad members list.
    """
    return {
        "type": "transferCall",
        "function": {
            "name": "transferCall",
            "description": "Transfer the prospect to the appropriate niche demo assistant based on their business type.",
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {
                        "type": "string",
                        "enum": ["dental", "motor_factors"],
                        "description": "Which demo to transfer to. Use 'dental' for dental/medical/health. Use 'motor_factors' for garages/motor trade/auto parts.",
                    }
                },
                "required": ["destination"],
            },
        },
        "destinations": [
            {
                "type": "assistant",
                "assistantName": "dental",
                "message": "Patching you through to Sarah at Bright Smile Dental now — she's ready for you.",
                "description": "Transfer to dental practice demo",
            },
            {
                "type": "assistant",
                "assistantName": "motor_factors",
                "message": "Connecting you to Murphy's Motor Factors now — go ahead whenever you're ready.",
                "description": "Transfer to motor factors demo",
            },
        ],
    }


CLAIRE_FIRST_MESSAGE = "Hi there! Thanks for ringing CallMe.ie. I'm Claire — can I ask, what type of business do you run?"


def create_claire(dental_id, motor_id):
    print("\n[2/3] Creating Claire (qualifier) assistant...")

    transfer_tool = build_claire_transfer_tool()
    prompt = build_claire_prompt(dental_id, motor_id)

    payload = {
        "name": "Claire — CallMe.ie Demo Qualifier",
        "firstMessage": CLAIRE_FIRST_MESSAGE,
        "model": {
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "messages": [{"role": "system", "content": prompt}],
            "temperature": 0.5,
            "maxTokens": 150,
            "tools": [transfer_tool],
        },
        "voice": {
            "provider": "11labs",
            "voiceId": "EXAVITQu4vr4xnSDxMaL",  # Sarah — warm, clear, works well for Irish context
            "stability": 0.5,
            "similarityBoost": 0.75,
            "style": 0.2,
            "useSpeakerBoost": True,
        },
        "transcriber": {
            "provider": "deepgram",
            "model": "nova-3",
            "language": "en",
            "smartFormat": True,
            "keywords": [
                "dental:2",
                "dentist:2",
                "garage:1",
                "salon:1",
                "solicitor:1",
                "restaurant:1",
            ],
        },
        "endCallPhrases": ["goodbye", "bye bye", "thanks, bye", "talk soon"],
        "silenceTimeoutSeconds": 25,
        "maxDurationSeconds": 300,
        "backgroundDenoisingEnabled": True,
        "serverUrl": f"{WEBHOOK_BASE}/vapi/call-ended",
    }

    result = vapi_post("/assistant", payload)
    assistant_id = result["id"]
    print(f"  OK Created: {assistant_id}")
    return assistant_id


# ---------------------------------------------------------------------------
# 3. UPDATE DENTAL ASSISTANT — Add PRSI + medical card
# ---------------------------------------------------------------------------
DENTAL_PRSI_ADDITION = """
PRSI DENTAL BENEFIT (Treatment Benefit Scheme):
PRSI contributors are entitled to:
- A free dental examination once per calendar year
- A scale and polish for €15 (heavily subsidised)
Eligibility: must have paid enough PRSI contributions (check via MyWelfare.ie or call DSP)
If caller asks "do I still have my PRSI?": tell them to check MyWelfare.ie or ring us and we'll check for them at the appointment — we just need their PPS number and date of birth.

MEDICAL CARD DENTAL:
Medical card holders are entitled to dental treatment under the DTSS scheme. This covers:
- Examinations
- Extractions
- Fillings (one surface, anterior teeth)
- Dentures (every 5 years)
For more complex treatment on a medical card, there's a referral process via the HSE.
If they ask "do you take medical cards?": "Yes, we do. We'll confirm your entitlements at the appointment — just bring your card."

PRIVATE HEALTH INSURANCE:
We accept: VHI, Laya Healthcare, Irish Life Health, Aviva, HSF Health Plan.
Patients can claim back on their plan — we can provide receipts. We don't always bill insurers directly, so advise them to check their policy.

COMMON IRISH DENTAL QUESTIONS:
- "Are you taking new patients?" → "We are, yes — let me check what's available for you."
- "Do you have any cancellations?" → "Let me check — is there a particular day that suits you?"
- "Do you do Invisalign / whitening / implants?" → "We do [service] — would you like to book a consultation?"
- "How much is a check-up?" → "A routine check-up is [price if known, else 'I'll get the team to send you our fee guide — what's your email?']"
"""


def update_dental_assistant():
    print(f"\n[3/3] Updating dental assistant ({DENTAL_ASSISTANT_ID}) with PRSI/medical card handling...")

    # Get current assistant config
    current = vapi_get(f"/assistant/{DENTAL_ASSISTANT_ID}")
    current_messages = current.get("model", {}).get("messages", [])

    # Append PRSI section to system prompt
    for msg in current_messages:
        if msg.get("role") == "system":
            msg["content"] = msg["content"] + "\n\n" + DENTAL_PRSI_ADDITION.strip()
            break

    payload = {
        "model": {
            **current.get("model", {}),
            "messages": current_messages,
        }
    }

    result = vapi_patch(f"/assistant/{DENTAL_ASSISTANT_ID}", payload)
    print(f"  OK Updated dental assistant with PRSI/medical card handling")
    return DENTAL_ASSISTANT_ID


# ---------------------------------------------------------------------------
# 4. CREATE VAPI SQUAD — wires Claire + demos together (no extra phone numbers)
# ---------------------------------------------------------------------------

def create_demo_squad(claire_id, dental_id, motor_id):
    """
    A Vapi Squad allows assistant-to-assistant handoffs on a single phone number.
    Claire is the entry point; she can hand off to dental or motor_factors.
    The assistantName values must match the 'destination' enum in Claire's transferCall tool.
    """
    print("\n[4/4] Creating demo squad (Claire + Dental + Motor Factors)...")

    payload = {
        "name": "CallMe.ie Demo Squad",
        "members": [
            {
                "assistantId": claire_id,
                # Claire can hand off to both demo assistants
                "assistantDestinations": [
                    {
                        "assistantName": "dental",
                        "message": "Patching you through to Sarah at Bright Smile Dental now.",
                        "description": "Transfer to dental demo",
                    },
                    {
                        "assistantName": "motor_factors",
                        "message": "Connecting you to Murphy's Motor Factors now.",
                        "description": "Transfer to motor factors demo",
                    },
                ],
            },
            {
                "assistantId": dental_id,
                "assistantName": "dental",
                # Dental can hand back to Claire if needed (end of demo)
                "assistantDestinations": [],
            },
            {
                "assistantId": motor_id,
                "assistantName": "motor_factors",
                "assistantDestinations": [],
            },
        ],
    }

    result = vapi_post("/squad", payload)
    squad_id = result["id"]
    print(f"  OK Created squad: {squad_id}")
    return squad_id


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    print("=" * 60)
    print("CallMe.ie Demo Assistant Setup")
    print("=" * 60)

    # Step 1: Create Motor Factors demo
    motor_id = create_motor_factors()

    # Step 2: Create Claire with transfer targets
    claire_id = create_claire(DENTAL_ASSISTANT_ID, motor_id)

    # Step 3: Update dental with PRSI handling
    update_dental_assistant()
    # Step 4: Create squad
    squad_id = create_demo_squad(claire_id, DENTAL_ASSISTANT_ID, motor_id)

    # Output summary
    print("\n" + "=" * 60)
    print("SETUP COMPLETE")
    print("=" * 60)
    print(f"\nClaire (qualifier):          {claire_id}")
    print(f"Murphy's Motor Factors:      {motor_id}")
    print(f"Bright Smile Dental:         {DENTAL_ASSISTANT_ID}")
    print(f"Demo Squad ID:               {squad_id}")
    print()
    print("NEXT STEPS:")
    print(f"1. In Vapi dashboard → Phone Numbers → assign {claire_id} to +1 (661) 764-3212")
    print(f"   Set Squad (not assistant) on the phone number")
    print()
    print("2. Add to Render CLIENTS_JSON env var:")
    clients_json = {
        "0b37deb5-2fc2-4e7b-81b1-e61e97103506": {
            "name": "Bright Smile Dental",
            "owner": "+353857863564",
            "from": "+16617643212",
            "calendar_id": "primary"
        },
        "9d91033c-cbcc-4e30-8a57-13bef92aecd3": {
            "name": "Bright Smile Dental",
            "owner": "+353857863564",
            "from": "+16617643212",
            "calendar_id": "primary"
        },
        claire_id: {
            "name": "CallMe.ie Demo",
            "owner": "+353857863564",
            "from": "+16617643212"
        },
        motor_id: {
            "name": "Murphy's Motor Factors",
            "owner": "+353857863564",
            "from": "+16617643212"
        },
    }
    print(json.dumps(clients_json, indent=2))
    print()
    print("3. Update SYSTEM.md with the new assistant IDs above")
    print()
    print("4. Test: ring +1 (661) 764-3212 — Claire should answer")
    print("   Say 'I run a dental practice' → should transfer to Sarah")
    print("   Say 'I'm in motor factors' → should transfer to Murphy's")

    # Save IDs to a file for SYSTEM.md update
    with open("demo-assistant-ids.json", "w") as f:
        json.dump({
            "claire_id": claire_id,
            "motor_factors_id": motor_id,
            "dental_id": DENTAL_ASSISTANT_ID,
            "clients_json": clients_json
        }, f, indent=2)
    print("\nIDs saved to demo-assistant-ids.json")


if __name__ == "__main__":
    main()
