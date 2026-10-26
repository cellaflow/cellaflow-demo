# The demo's infrastructure: one instance, a small disk for its certificates, one Elastic IP, one
# firewall. `terraform destroy` removes them all. The DNS records live in Cloudflare and are created
# by hand (README, Deploy).

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
  # The files the instance runs, listed once in deploy/files.txt so deploy/push.sh ships the same
  # set. A path under deploy/ lands at the top of /opt/demo; any other keeps its path.
  file_list = compact(split("\n", file("${path.module}/../deploy/files.txt")))
  files     = { for p in local.file_list : trimprefix(p, "deploy/") => file("${path.module}/../${p}") }
  # Compose reads this as /opt/demo/.env. Written at first boot and by deploy/push.sh.
  env_file = <<-ENV
    DEMO_HOSTNAME=${local.hostname}
    DEMO_HOSTNAMES=${join(", ", concat([local.hostname], var.extra_hostnames))}
    DEMO_ALLOWED_ORIGINS=${var.allowed_origins}
    DEMO_AWS_REGION=${var.region}
  ENV
}

resource "aws_instance" "demo" {
  ami                         = data.aws_ami.ubuntu.id
  instance_type               = var.instance_type
  availability_zone           = var.availability_zone
  vpc_security_group_ids      = [aws_security_group.demo.id]
  iam_instance_profile        = aws_iam_instance_profile.demo.name
  associate_public_ip_address = true

  # Used at first boot only. Code changes reach the running instance through deploy/push.sh.
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    hostname        = local.hostname
    env_file        = local.env_file
    caddy_volume_id = aws_ebs_volume.caddy.id
    files_b64       = { for name, content in local.files : name => base64gzip(content) }
  })

  # Neither a code change nor a new Ubuntu release replaces the instance. A replacement would cost a
  # few minutes of downtime; the certificates survive it either way (aws_ebs_volume.caddy).
  lifecycle {
    ignore_changes = [user_data, ami]
  }

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

# --- Certificates ---------------------------------------------------------------------------------
# Caddy's data (certificates and the ACME account) lives on its own disk, so a new instance reuses
# the certificates instead of requesting new ones. Let's Encrypt issues only five per hostname per
# week, and replacing the instance used to spend one of them each time.

resource "aws_ebs_volume" "caddy" {
  availability_zone = var.availability_zone
  size              = 1
  type              = "gp3"
  encrypted         = true
  tags = {
    Name = "cellaflow-demo-certificates"
  }
}

resource "aws_volume_attachment" "caddy" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.caddy.id
  instance_id = aws_instance.demo.id
  # On a replacement, the old instance stops before the disk moves, so Caddy is not writing to it.
  stop_instance_before_detaching = true
}

# --- Logs -----------------------------------------------------------------------------------------
# The engine and the API send their output here through Docker's awslogs driver. Stream with:
#   aws logs tail /cellaflow-demo/engine --follow --profile <your profile>

resource "aws_cloudwatch_log_group" "demo" {
  for_each          = toset(["engine", "api"])
  name              = "/cellaflow-demo/${each.key}"
  retention_in_days = 14
}

resource "aws_iam_role" "demo" {
  name = "cellaflow-demo-instance"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Write to the two log groups and nothing else.
resource "aws_iam_role_policy" "logs" {
  name = "write-demo-logs"
  role = aws_iam_role.demo.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = [for g in aws_cloudwatch_log_group.demo : "${g.arn}:*"]
    }]
  })
}

# A shell when one is needed, without opening SSH: aws ssm start-session --target <instance_id>
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.demo.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "demo" {
  name = "cellaflow-demo-instance"
  role = aws_iam_role.demo.name
}

resource "aws_eip" "demo" {
  instance = aws_instance.demo.id
  domain   = "vpc"
  tags = {
    Name = "cellaflow-demo"
  }
}
