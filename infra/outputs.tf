output "url" {
  value = "https://${local.hostname}"
}

output "public_ip" {
  description = "Point the Cloudflare A record (DNS only) for the hostname here"
  value       = aws_eip.demo.public_ip
}

output "logs" {
  description = "Stream the engine's logs (swap engine for api to see the API)"
  value       = "aws logs tail /cellaflow-demo/engine --follow --profile <your profile>"
}

output "instance_id" {
  description = "Boot log: aws ec2 get-console-output --instance-id <id> --profile <your profile>"
  value       = aws_instance.demo.id
}

output "region" {
  description = "Read by deploy/push.sh"
  value       = var.region
}

output "env_file" {
  description = "The instance's /opt/demo/.env. Read by deploy/push.sh"
  value       = local.env_file
}
