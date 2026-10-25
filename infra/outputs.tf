output "url" {
  value = "https://${local.hostname}"
}

output "public_ip" {
  description = "Point the Cloudflare A record (DNS only) for the hostname here"
  value       = aws_eip.demo.public_ip
}

output "instance_id" {
  description = "Boot log: aws ec2 get-console-output --instance-id <id> --profile <your profile>"
  value       = aws_instance.demo.id
}
