# The demo's infrastructure: one instance, one Elastic IP, one firewall. `terraform destroy`
# removes all three. The DNS record lives in Cloudflare and is created by hand (README, Deploy).

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = {
      project = "cellaflow-demo"
      managed = "terraform"
    }
  }
}

data "aws_vpc" "default" {
  default = true
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"]
  }
}

resource "aws_security_group" "demo" {
  name        = "cellaflow-demo"
  description = "Demo: HTTPS for the API, HTTP for certificate issuance and redirect"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTP, for ACME and the redirect to HTTPS"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "Image pulls, packages, ACME"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

locals {
  hostname = "${var.subdomain}.${var.zone_name}"
  files = {
    "compose.prod.yml"     = file("${path.module}/../deploy/compose.prod.yml")
    "Caddyfile"            = file("${path.module}/../deploy/Caddyfile")
    "wipe-engine.sh"       = file("${path.module}/../deploy/wipe-engine.sh")
    "api/app.py"           = file("${path.module}/../api/app.py")
    "api/requirements.txt" = file("${path.module}/../api/requirements.txt")
    "api/Dockerfile"       = file("${path.module}/../api/Dockerfile")
  }
}

resource "aws_instance" "demo" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  vpc_security_group_ids      = [aws_security_group.demo.id]
  associate_public_ip_address = true
  # A change to any file rebuilds the instance. The engine's data is disposable by design.
  user_data_replace_on_change = true

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    hostname        = local.hostname
    allowed_origins = var.allowed_origins
    files_b64       = { for name, content in local.files : name => base64gzip(content) }
  })

  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size = 12
    volume_type = "gp3"
    encrypted   = true
  }

  tags = {
    Name = "cellaflow-demo"
  }
}

resource "aws_eip" "demo" {
  instance = aws_instance.demo.id
  domain   = "vpc"
  tags = {
    Name = "cellaflow-demo"
  }
}
