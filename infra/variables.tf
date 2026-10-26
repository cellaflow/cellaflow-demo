variable "aws_profile" {
  description = "AWS CLI profile Terraform uses. Set it in terraform.tfvars, which is not committed."
  type        = string
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "zone_name" {
  description = "The domain the demo's hostname is under (DNS is in Cloudflare)"
  type        = string
  default     = "cellaflow.com"
}

variable "subdomain" {
  type    = string
  default = "demo"
}

variable "instance_type" {
  description = "2 GB: every Cellaflow agent is its own process, about 13 MiB each, up to 6 runs of 8"
  type        = string
  default     = "t4g.small"
}

variable "allowed_origins" {
  description = "Origins the browser may call the API from"
  type        = string
  default     = "https://cellaflow.com,https://www.cellaflow.com,http://localhost:3000"
}
