#!/usr/bin/env bash
# Swap META_AD_ACCOUNT_ID in vault + Coolify env + restart container.
# Usage: ./swap_ad_account.sh act_NEW_ID

set -e
NEW_ID="${1:?usage: $0 act_<id>}"
case "$NEW_ID" in
  act_*) ;;
  *) NEW_ID="act_$NEW_ID" ;;
esac

KEY="$HOME/.ssh/owl_deploy_ed25519"
CID="xml9wji6109b1kergfz05665-144849264376"
HOST="root@178.104.205.255"
COOLIFY_ENV="/data/coolify/applications/xml9wji6109b1kergfz05665/.env"
VAULT="$HOME/.claude/routes/.env"

echo "=== 1. update local vault ==="
sed -i "s|^META_AD_ACCOUNT_ID=.*|META_AD_ACCOUNT_ID=$NEW_ID|" "$VAULT"
grep '^META_AD_ACCOUNT_ID=' "$VAULT"

echo ""
echo "=== 2. update Coolify env on VPS ==="
ssh -o StrictHostKeyChecking=no -i "$KEY" "$HOST" "
  if grep -q '^META_AD_ACCOUNT_ID=' $COOLIFY_ENV; then
    sed -i 's|^META_AD_ACCOUNT_ID=.*|META_AD_ACCOUNT_ID=$NEW_ID|' $COOLIFY_ENV
  else
    echo 'META_AD_ACCOUNT_ID=$NEW_ID' >> $COOLIFY_ENV
  fi
  grep '^META_AD_ACCOUNT_ID=' $COOLIFY_ENV
"

echo ""
echo "=== 3. recreate container w/ new env (docker compose up -d picks up .env changes) ==="
ssh -o StrictHostKeyChecking=no -i "$KEY" "$HOST" "
  cd /data/coolify/applications/xml9wji6109b1kergfz05665 && docker compose --env-file .env up -d
  sleep 4
  docker logs --tail 10 $CID
"

echo ""
echo "=== 4. assign System User to new account ==="
TOK=$(grep '^META_API=' "$VAULT" | cut -d= -f2)
SU="122095526283319685"
curl -s -X POST "https://graph.facebook.com/v25.0/$NEW_ID/assigned_users" \
  -d "user=$SU" \
  -d 'tasks=["MANAGE","ADVERTISE","ANALYZE"]' \
  -d "access_token=$TOK"
echo ""

echo ""
echo "=== 5. verify smoke test ==="
ADMIN_TOK=$(ssh -o StrictHostKeyChecking=no -i "$KEY" "$HOST" "docker exec $CID sh -c 'echo \$ADMIN_TOKEN'")
curl -s "https://api.callmeie.ie/admin/api/ads/account?token=$ADMIN_TOK" | head -c 500
echo ""

echo ""
echo "=== 6. retry 4 PAUSED drafts ==="
PAGE="1105012356028968"
for tpl in audit receptionist docops websites; do
  echo "--- $tpl ---"
  curl -s -X POST "https://api.callmeie.ie/admin/api/ads/draft?token=$ADMIN_TOK" \
    -H "Content-Type: application/json" \
    -d "{\"template_key\":\"$tpl\",\"page_id\":\"$PAGE\",\"daily_budget_usd\":2.0}" | head -c 400
  echo ""
  sleep 2
done

echo ""
echo "DONE. New account: $NEW_ID"
