# admin.html endpoint coverage notes

Primary chrome:

- Today: `/admin/api/today-actions`, `/admin/api/operations-summary`, `/admin/api/health-detail`, `/admin/api/events`, `/admin/api/recordings-enriched`, drawer calls to `/admin/api/caller/{call_id}`, `/admin/api/send-setup-link`, `/admin/api/stripe-recent`, and onboarding provision/reject routes when an action has a submission id.
- Leads: `/admin/api/leads-unified`, `/admin/api/leads`, and `PATCH /admin/api/leads/{id}` for inline status changes.
- Calls: `/admin/api/events`, `/admin/api/call-scoring`, `/admin/api/send-setup-link`, and drawer `/admin/api/caller/{call_id}`.
- Books: `/admin/api/stripe-recent` and `/admin/api/operations-summary`.

More menu:

- Queue: `/admin/api/submissions`, `/admin/api/provision/{id}`, `/admin/api/reject/{id}`.
- Recordings: `/admin/api/recordings-enriched` with `/admin/api/recordings` fallback.
- Errors: `/admin/api/vendor-errors-unified`.
- Assistants: `/admin/api/vapi/assistants`, `/admin/api/vapi/assistant/{id}`, and prompt/voice/keyterms save endpoints.
- Flow: `/admin/api/flow-graph`.
- Active clients: `/admin/api/clients`.
- Discovery: `/admin/api/discovery-submissions`.
- Ads: `/admin/api/ads/account`, `/admin/api/ads/templates`, `/admin/api/ads/pages`, `/admin/api/ads/campaigns`, `/admin/api/ads/canary`, draft/activate/pause endpoints.
- Inbox: `/admin/api/leads-unified`.
- Settings: `/admin/api/heat-by-assistant`, `/admin/api/twilio-numbers`, `/admin/api/test-sms`, `/admin/api/coolify-redeploy`, `/admin/api/admin-token-rotate`, `/admin/api/data-export`.

PWA/auth/status:

- Token flow is unchanged: `?token=` is saved to `localStorage.admin_token`, then stripped from the URL. Every admin fetch appends `?token=...`.
- Theme is controlled by `localStorage.admin_theme` with `light`, `dark`, and `auto`.
- Health favicon/title status is still driven by `/admin/api/health-detail`.
