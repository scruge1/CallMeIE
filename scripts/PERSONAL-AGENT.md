# Adam personal agent

Admin tab: /admin#personal-agent. Dedicated assistant 5e192096-091e-4f18-9b0b-4d9a80c4c637 on +35361788358. Personal and CallMeIE messages; no booking.

personal_agent.py installs a dedicated router in server.py. personal-agent.js/css implement the nested workspace. Existing call_events/call_notes store context. Three additive tables store configuration/version/secret, intake/follow-up and notification outcomes. All personal admin API routes require the existing admin token. No client token is issued. Dedicated x-personal-agent-secret authenticates Vapi hooks. Destinations are not model parameters. Private notes are excluded from published prompts. Vapi updates target only the dedicated assistant.

Tests: scripts/tests/test_personal_agent.py. Source dependencies remain unchanged. Dockerfile copies the new assets. Shared client HTML and demo routing stay unchanged.

Existing Coolify application: xml9wji6109b1kergfz05665. GitHub: scruge1/CallMeIE. Previous deployed commit: 02f99ea822d26bcf3431d7a829dc9c80ac43eb41. If rolling back code, restore the dedicated assistant's prior configuration too. Retain persisted messages/notes.

Open limits: carrier conditional forwarding, enforced retention controls, automatic delivery retries/digests and verified caller identity. Notifications go to Adam only. Do not import private operator context into the voice knowledge.
