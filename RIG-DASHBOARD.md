# Private rig dashboard

The CallMeIE client portal has an operator-only `Rig` tab. It is hidden unless
the authenticated client matches an explicit rig allow-list.

## Monitoring configuration

Set these as deployment environment variables. Do not commit them to a file:

```text
RIG_DASHBOARD_TENANTS=adam
RIG_MONITORING_API_URL=http://100.84.3.33:9091
RIG_GRAFANA_URL=http://100.84.3.33:3006
```

`RIG_MONITORING_API_URL` must be reachable from the CallMeIE backend. A
browser-only Tailscale route is not sufficient because the backend performs the
Prometheus query. `RIG_DASHBOARD_CLIENT_TOKEN` may be used instead of, or in
addition to, `RIG_DASHBOARD_TENANTS` when a dedicated client token is preferred.

## Recommended private-network mode

The Coolify host cannot currently reach the rig's private addresses. In that
case, leave `RIG_MONITORING_API_URL` unset and configure one secret on both
ends:

```text
# CallMeIE deployment environment
RIG_INGEST_TOKEN=<secret-managed-value>
RIG_DASHBOARD_TENANTS=adam

# Rig: /etc/rig-monitoring/ingest.env, mode 600
RIG_PROMETHEUS_URL=http://100.84.3.33:9091
RIG_INGEST_URL=https://api.callmeie.ie/api/rig/ingest
RIG_INGEST_TOKEN=<same-secret-managed-value>
```

Install `rig-health-push.py`, `rig-health-push.service`, and
`rig-health-push.timer` from the monitoring workspace on the rig. The push
contains only the existing credential-free Prometheus snapshot. The CallMeIE
backend stores the latest snapshot in its database and the mobile dashboard
reads that cached copy.

Push mode enables telemetry only. BMC power controls remain hidden until a
separate private BMC route or an explicitly reviewed power relay is available.

The vLLM container is healthy and exposes `/metrics` on port 8000. The rig now
has a verified `vllm` Prometheus scrape target, so the dashboard can report the
service as running when the target is up.

## Optional power controls

Leave power control disabled until the backend has a private route to the BMC
and a dedicated BMC account with only the required permission:

```text
RIG_POWER_CONTROL_ENABLED=true
RIG_BMC_URL=https://192.168.1.76
RIG_BMC_SYSTEM_PATH=/redfish/v1/Systems/1
RIG_BMC_USERNAME=<secret-managed-value>
RIG_BMC_PASSWORD=<secret-managed-value>
RIG_BMC_CA_BUNDLE=<path-to-trusted-bmc-ca-or-empty-for-local-cert>
```

The dashboard exposes `On` and `GracefulShutdown`. It does not expose
`ForceOff`. Power-down also requires the exact typed confirmation
`POWER DOWN EPYC RIG`.

The server reads BMC credentials only from the process environment. It does not
return them to the browser, write them to logs, or store them in the repository.
