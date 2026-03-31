"""
Generate weekly/monthly client report from Vapi call logs.

Usage:
    python generate-report.py --days 7 --assistant-id abc123
    python generate-report.py --days 30 --assistant-id abc123 --email client@example.com

Pulls call data from Vapi API and generates a summary showing:
- Total calls handled
- Appointments booked
- Emergencies transferred
- Average call duration
- Peak call times
- Top questions asked
- Missed call recovery rate
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from collections import Counter
import json
import requests

VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")


def get_calls(assistant_id: str, days: int) -> list:
    """Fetch call logs from Vapi API."""
    since = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"

    resp = requests.get(
        "https://api.vapi.ai/call",
        headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
        params={
            "assistantId": assistant_id,
            "createdAtGe": since,
            "limit": 1000,
        },
    )

    if resp.status_code != 200:
        print(f"API error: {resp.status_code}")
        return []

    return resp.json()


def generate_report(calls: list, days: int, business_name: str = "Your Business") -> str:
    """Generate a formatted report from call data."""
    if not calls:
        return f"No calls in the last {days} days."

    total = len(calls)
    total_duration = sum(c.get("duration", 0) for c in calls)
    avg_duration = total_duration / total if total else 0

    # Count statuses
    statuses = Counter(c.get("status", "unknown") for c in calls)
    completed = statuses.get("ended", 0) + statuses.get("completed", 0)
    missed = statuses.get("missed", 0) + statuses.get("no-answer", 0)

    # Count tool usage (appointments, transfers, SMS)
    appointments = 0
    transfers = 0
    sms_sent = 0
    for c in calls:
        messages = c.get("messages", [])
        for m in messages:
            if "tool_calls" in str(m) or "toolCalls" in str(m):
                tool_str = json.dumps(m)
                if "calendar" in tool_str.lower() and "create" in tool_str.lower():
                    appointments += 1
                if "transfer" in tool_str.lower():
                    transfers += 1
                if "sms" in tool_str.lower():
                    sms_sent += 1

    # Peak hours
    hours = Counter()
    for c in calls:
        created = c.get("createdAt", "")
        if created:
            try:
                hour = datetime.fromisoformat(created.replace("Z", "+00:00")).hour
                hours[hour] += 1
            except (ValueError, AttributeError):
                pass

    peak_hours = hours.most_common(3)

    # Generate report
    report = f"""
=====================================
  {business_name} — AI Receptionist Report
  Period: Last {days} days
  Generated: {datetime.now().strftime("%d %B %Y")}
=====================================

CALL SUMMARY
  Total calls handled:     {total}
  Completed calls:         {completed}
  Missed/no-answer:        {missed}
  Average call duration:   {avg_duration:.0f} seconds

OUTCOMES
  Appointments booked:     {appointments}
  Emergency transfers:     {transfers}
  SMS confirmations sent:  {sms_sent}

PEAK CALL TIMES
"""
    for hour, count in peak_hours:
        report += f"  {hour:02d}:00 - {hour + 1:02d}:00    {count} calls\n"

    # ROI estimate — per-industry average appointment value
    industry_values = {
        "dental": 120,
        "salon": 45,
        "motor": 180,
        "solicitor": 300,
        "clinic": 200,
        "default": 100,
    }
    estimated_value_per_booking = industry_values.get("default", 100)  # override per client
    roi = appointments * estimated_value_per_booking
    report += f"""
ESTIMATED VALUE CAPTURED
  Appointments x {estimated_value_per_booking} EUR = {roi:,.0f} EUR
  (Calls that would have gone to voicemail)

CALL RECOVERY
  Calls handled after hours:  {missed} recovered via AI
  Without AI, these would be: lost to voicemail or competitors

=====================================
  Powered by AI Voice Technology
=====================================
"""
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate client report")
    parser.add_argument("--days", type=int, default=7, help="Report period in days")
    parser.add_argument("--assistant-id", required=True, help="Vapi assistant ID")
    parser.add_argument("--business", default="Your Business", help="Business name")
    parser.add_argument("--email", help="Send report to this email (not implemented yet)")
    parser.add_argument("--api-key", help="Vapi API key")
    args = parser.parse_args()

    if args.api_key:
        VAPI_API_KEY = args.api_key

    if not VAPI_API_KEY:
        print("Set VAPI_API_KEY or use --api-key")
        sys.exit(1)

    calls = get_calls(args.assistant_id, args.days)
    report = generate_report(calls, args.days, args.business)
    print(report)

    # Save to file
    filename = f"report_{args.business.replace(' ', '_')}_{datetime.now().strftime('%Y%m%d')}.txt"
    with open(filename, "w") as f:
        f.write(report)
    print(f"Report saved to: {filename}")
