terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = { project = var.project, managed_by = "terraform" }
  }
}

data "aws_caller_identity" "me" {}

# AL2023 x86_64 AMI
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  account   = data.aws_caller_identity.me.account_id
  topic_arn = "${replace(var.msk_cluster_arn, ":cluster/", ":topic/")}/*"
  group_arn = "${replace(var.msk_cluster_arn, ":cluster/", ":group/")}/*"
}

# =============================================================================
# Code delivery — tarball to S3, pulled + built on the instance at boot
# =============================================================================
resource "aws_s3_bucket" "deploy" {
  bucket        = "${var.project}-deploy-${local.account}"
  force_destroy = true
}

# Lock the deploy bucket down: no public access, SSE at rest, bucket-owner-only.
resource "aws_s3_bucket_public_access_block" "deploy" {
  bucket                  = aws_s3_bucket.deploy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_ownership_controls" "deploy" {
  bucket = aws_s3_bucket.deploy.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

# Reject any non-TLS access to the bucket / objects.
resource "aws_s3_bucket_policy" "deploy_tls_only" {
  bucket     = aws_s3_bucket.deploy.id
  depends_on = [aws_s3_bucket_public_access_block.deploy]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.deploy.arn, "${aws_s3_bucket.deploy.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_s3_object" "code" {
  bucket = aws_s3_bucket.deploy.id
  key    = "acme-ofv-deploy.tar.gz"
  source = "${path.module}/acme-ofv-deploy.tar.gz"
  etag   = filemd5("${path.module}/acme-ofv-deploy.tar.gz")
}

# =============================================================================
# Security groups
# =============================================================================
resource "aws_security_group" "alb" {
  name        = "${var.project}-alb-sg"
  description = "ALB ingress"
  vpc_id      = var.vpc_id

  ingress {
    description = "API HTTPS via ALB :443 (allow-listed sources only)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  ingress {
    description = "API :80 redirect to :443 (allow-listed sources only)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  ingress {
    description = "mock OFP HTTPS via ALB :8100 (allow-listed sources only)"
    from_port   = 8100
    to_port     = 8100
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-alb-sg" }
}

resource "aws_security_group" "app" {
  name        = "${var.project}-app-sg"
  description = "EC2 app instance"
  vpc_id      = var.vpc_id

  ingress {
    description     = "api 8010 from ALB"
    from_port       = 8010
    to_port         = 8010
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  ingress {
    description     = "mock 8100 from ALB"
    from_port       = 8100
    to_port         = 8100
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "${var.project}-app-sg" }
}

# Allow the app instance to reach MSK on the IAM port (additive rule on the
# existing shared MSK security group). Atlas PrivateLink already admits the VPC
# CIDR, so no change is needed there.
resource "aws_security_group_rule" "msk_from_app" {
  type                     = "ingress"
  description              = "${var.project} app to MSK IAM 9098"
  from_port                = 9098
  to_port                  = 9098
  protocol                 = "tcp"
  security_group_id        = var.msk_security_group_id
  source_security_group_id = aws_security_group.app.id
}

# =============================================================================
# IAM — SSM + CloudWatch + MSK IAM + Secrets Manager + S3 (deploy bucket)
# =============================================================================
resource "aws_iam_role" "app" {
  name = "${var.project}-app-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cw" {
  role       = aws_iam_role.app.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy" "app_inline" {
  name = "${var.project}-app-inline"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "MSKConnect"
        Effect   = "Allow"
        Action   = ["kafka-cluster:Connect", "kafka-cluster:DescribeCluster"]
        Resource = [var.msk_cluster_arn]
      },
      {
        Sid    = "MSKTopicReadWrite"
        Effect = "Allow"
        Action = [
          "kafka-cluster:CreateTopic", "kafka-cluster:DescribeTopic",
          "kafka-cluster:WriteData", "kafka-cluster:ReadData",
          "kafka-cluster:AlterTopic",
        ]
        Resource = [local.topic_arn]
      },
      {
        Sid      = "MSKGroup"
        Effect   = "Allow"
        Action   = ["kafka-cluster:AlterGroup", "kafka-cluster:DescribeGroup"]
        Resource = [local.group_arn]
      },
      {
        Sid      = "Secrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:${local.account}:secret:${var.secret_name}-*"
      },
      {
        Sid      = "DeployBucket"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.deploy.arn}/*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "app" {
  name = "${var.project}-app-profile"
  role = aws_iam_role.app.name
}

# =============================================================================
# TLS — self-signed cert imported into ACM for the ALB HTTPS listeners.
#
# The ALB only has an AWS-generated *.elb.amazonaws.com DNS name, which a public
# CA / ACM-managed cert cannot be issued for, so we self-sign. The local Vite dev
# proxy reaches the ALB server-side with `secure: false`, so the browser only
# ever talks to localhost and never sees a cert warning. To use a trusted cert
# later, point a custom domain at the ALB and swap in an aws_acm_certificate
# (DNS-validated) here — nothing else changes.
# =============================================================================
resource "tls_private_key" "alb" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "alb" {
  private_key_pem = tls_private_key.alb.private_key_pem

  subject {
    common_name  = "${var.project}.internal"
    organization = "Acme OFV POC"
  }

  dns_names             = ["*.${var.aws_region}.elb.amazonaws.com"]
  validity_period_hours = 8760 # 1 year
  early_renewal_hours   = 720

  allowed_uses = ["key_encipherment", "digital_signature", "server_auth"]
}

resource "aws_acm_certificate" "alb" {
  private_key      = tls_private_key.alb.private_key_pem
  certificate_body = tls_self_signed_cert.alb.cert_pem
  tags             = { Name = "${var.project}-alb-cert" }

  lifecycle {
    create_before_destroy = true
  }
}

# =============================================================================
# ALB + target groups + listeners
# =============================================================================
resource "aws_lb" "main" {
  name               = "${var.project}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnet_ids
}

resource "aws_lb_target_group" "api" {
  name     = "${var.project}-api-tg"
  port     = 8010
  protocol = "HTTP"
  vpc_id   = var.vpc_id
  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_target_group" "mock" {
  name     = "${var.project}-mock-tg"
  port     = 8100
  protocol = "HTTP"
  vpc_id   = var.vpc_id
  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 5
  }
}

resource "aws_lb_target_group_attachment" "api" {
  target_group_arn = aws_lb_target_group.api.arn
  target_id        = aws_instance.app.id
  port             = 8010
}

resource "aws_lb_target_group_attachment" "mock" {
  target_group_arn = aws_lb_target_group.mock.arn
  target_id        = aws_instance.app.id
  port             = 8100
}

# :80 exists only to 301-redirect to :443 (encrypted communications only).
resource "aws_lb_listener" "api_http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "api" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.alb.arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "mock" {
  load_balancer_arn = aws_lb.main.arn
  port              = 8100
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.alb.arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mock.arn
  }
}

# =============================================================================
# EC2 instance (public subnet, in-VPC for MSK + Atlas PrivateLink)
# =============================================================================
resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = var.public_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.app.name
  associate_public_ip_address = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2
    http_put_response_hop_limit = 2          # containers must reach IMDS for IAM creds
  }

  root_block_device {
    volume_size = 40
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = base64encode(templatefile("${path.module}/user_data.sh.tftpl", {
    aws_region    = var.aws_region
    secret_name   = var.secret_name
    deploy_bucket = aws_s3_bucket.deploy.id
    deploy_key    = aws_s3_object.code.key
    kafka_brokers = var.kafka_brokers
  }))

  depends_on = [aws_s3_object.code]
  tags       = { Name = "${var.project}-app" }
}
