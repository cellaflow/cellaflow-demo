# cellaflow-demo

The live demo on cellaflow.com: several agents race to place one order, with and without
Cellaflow, against a real engine. Visitors can add agents while the order is in flight and see how
many orders actually went through.

Design, what is real and what is simulated, and the limits: [docs/design.md](docs/design.md).

```
api/       The demo API (FastAPI). Agents are real Python SDK clients of a real engine.
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

Any change to `api/` or `deploy/` rebuilds the instance on the next `apply`. The engine's data is
disposable, and is wiped daily at 09:00 UTC anyway.

Boot log: `aws ec2 get-console-output --instance-id <instance_id> --profile <your profile>`.

## Tear down

`terraform destroy`, then delete the Cloudflare record.

## License

[Apache-2.0](LICENSE).
