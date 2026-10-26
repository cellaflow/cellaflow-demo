#!/bin/bash
# Ships the demo's files to the running instance and restarts what changed, without replacing the
# instance. Takes about half a minute; the API is down for a few seconds while it restarts.
#
#   deploy/push.sh
#
# Uses Session Manager, so it needs no SSH and no open port. The AWS profile comes from
# infra/terraform.tfvars, or from AWS_PROFILE if set. Run `terraform apply` first if infra/ changed.
set -euo pipefail
cd "$(dirname "$0")/.."

profile=${AWS_PROFILE:-$(sed -n 's/^aws_profile *= *"\(.*\)"/\1/p' infra/terraform.tfvars)}
instance=$(terraform -chdir=infra output -raw instance_id)
region=$(terraform -chdir=infra output -raw region)
aws=(aws --profile "$profile" --region "$region")

# The same files Terraform ships at first boot (deploy/files.txt), plus the .env it renders. A path
# under deploy/ lands at the top of /opt/demo, as at boot.
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT
mkdir -p "$staging/api"
while read -r path; do
  [ -n "$path" ] && cp "$path" "$staging/${path#deploy/}"
done < deploy/files.txt
terraform -chdir=infra output -raw env_file > "$staging/.env"
# Named entries rather than ".", so unpacking does not reset /opt/demo's own permissions.
bundle=$(cd "$staging" && COPYFILE_DISABLE=1 tar --format ustar -czf - .env * | base64 | tr -d '\n')

# On the instance: unpack, rebuild and restart the engine and API if their files changed, and
# recreate Caddy if its config changed. Caddy reads the Caddyfile through a single-file bind mount,
# which keeps pointing at the old file once it is replaced, so a reload alone would not see it.
read -r -d '' script <<EOF || true
set -eu
cd /opt/demo
before=\$(md5sum Caddyfile .env | md5sum)
echo '$bundle' | base64 -d | tar -xz --no-same-owner -C /opt/demo
chmod +x wipe-engine.sh
docker compose -p demo -f compose.prod.yml up -d --build --wait engine api
if [ "\$(md5sum Caddyfile .env | md5sum)" != "\$before" ]; then
  docker compose -p demo -f compose.prod.yml up -d --force-recreate caddy
else
  docker compose -p demo -f compose.prod.yml up -d caddy
fi
docker compose -p demo -f compose.prod.yml ps --format '{{.Name}}: {{.Status}}'
EOF

params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.stdin.read()]}))' <<<"$script")
command_id=$("${aws[@]}" ssm send-command --instance-ids "$instance" \
  --document-name AWS-RunShellScript --comment "deploy/push.sh" \
  --parameters "$params" --query Command.CommandId --output text)
echo "Sent to $instance ($command_id). Waiting for it to finish..."

# `ssm wait` gives up after 100 seconds, and an image rebuild can take longer, so poll.
while :; do
  status=$("${aws[@]}" ssm get-command-invocation --command-id "$command_id" \
    --instance-id "$instance" --query Status --output text 2>/dev/null || echo Pending)
  case "$status" in
    Pending | InProgress | Delayed) sleep 5 ;;
    *) break ;;
  esac
done

"${aws[@]}" ssm get-command-invocation --command-id "$command_id" --instance-id "$instance" \
  --query '[StandardOutputContent, StandardErrorContent]' --output text
echo "Status: $status"
[ "$status" = Success ]
