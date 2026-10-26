# cellaflow-demo

The live demo on cellaflow.com: several agents race to place one order, with and without
Cellaflow, against a real engine. Visitors can add agents while the order is in flight, kill agents
and watch them recover, and see how many orders and confirmations actually went out.

Design, what is real and what is simulated, and the limits: [docs/design.md](docs/design.md).

```
api/       The demo API (FastAPI), and the agents (agents.py): real Python SDK clients of a real
           engine, one OS process each.
deploy/    Production compose file, Caddyfile, and the daily engine wipe.
infra/     Terraform for the AWS instance (us-east-1).
```

The page itself lives in `cellaflow-landing` and calls this API at `https://demo.cellaflow.com`.

## Run locally

```bash
docker compose up -d --build --wait
curl -s -X POST localhost:8000/api/runs -H 'content-type: application/json' -d '{"agents": 3}'
```

Tests run against the engine on the compose network:

```bash
docker run --rm --network cellaflow-demo_default -v "$PWD/api":/src:ro \
  -e ENGINE_TARGET=engine:50051 -e DEMO_RUNS_PER_IP_PER_MINUTE=100 python:3.12-slim \
  sh -c "cp -r /src /app && cd /app && pip install -q -r requirements.txt pytest httpx && python -m pytest -q"
```

## Deploy

```bash
cd infra
echo 'aws_profile = "<your AWS CLI profile>"' > terraform.tfvars   # not committed
terraform init
terraform apply          # prints public_ip
```

Then, in Cloudflare, create an A record for `demo.cellaflow.com` pointing at `public_ip`, **DNS
only** (not proxied). The instance waits for that record before requesting a certificate, so the
order does not matter, but HTTPS starts only once the record resolves.

To ship a change to `api/` or `deploy/`, push it to the running instance:

```bash
deploy/push.sh           # about 30 seconds, through Session Manager
```

It sends the files in `deploy/files.txt`, rebuilds what changed, and restarts the API (a few
seconds of downtime) and Caddy only if its config changed. `terraform apply` does not redeploy
code: the instance's boot script is used at first boot only, so a code change never replaces the
instance. Add a new file to `deploy/files.txt`, which both read.

Caddy's certificates live on their own 1 GB disk, which outlives the instance. If the instance is
replaced (`terraform apply -replace=aws_instance.demo`, or a change such as the zone), the new one
reattaches the disk and reuses them. That matters because Let's Encrypt issues only five
certificates per hostname per week. The engine's data stays on the instance; it is disposable, and
is wiped daily at 09:00 UTC anyway.

## Logs

The engine and the API send their output to CloudWatch, kept for 14 days:

```bash
aws logs tail /cellaflow-demo/engine --follow --profile <your profile>   # sessions and step commits
aws logs tail /cellaflow-demo/api --follow --profile <your profile>      # HTTP requests
```

Locally, `docker compose logs -f engine`.

A shell on the instance, without SSH: `aws ssm start-session --target <instance_id> --profile <your profile>`
(needs the Session Manager plugin for the AWS CLI). Boot log:
`aws ec2 get-console-output --instance-id <instance_id> --profile <your profile>`.

## Tear down

`terraform destroy`, then delete the Cloudflare record.

## License

[Apache-2.0](LICENSE).
