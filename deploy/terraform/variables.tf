variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
}

variable "project" {
  type    = string
  default = "acme-ofv"
}

# Source IPs allowed to reach the public ALB (api :443/:80-redirect, mock :8100).
# Closes the EXTERNAL_ATTACK_SURFACE finding: the ALB is no longer open to
# 0.0.0.0/0. Set this to your laptop's public IP (the one the Vite dev proxy
# egresses from) plus any teammates. Override in terraform.tfvars; the default
# below is a convenience only and WILL rotate if you are on a VPN / WARP.
#   curl -s https://checkip.amazonaws.com   # your current public IP
variable "allowed_cidrs" {
  type        = list(string)
  description = "CIDRs permitted to reach the public ALB listeners."
  default     = ["203.0.113.10/32"]
}

# --- reused estate (data, not created) ---
variable "vpc_id" {
  type    = string
  default = "vpc-xxxxxxxx"
}

# public subnets (≥2 AZs for the ALB). EC2 lands in the first one.
variable "public_subnet_ids" {
  type    = list(string)
  default = ["subnet-aaaaaaaa", "subnet-bbbbbbbb", "subnet-cccccccc"]
}

variable "msk_security_group_id" {
  type    = string
  default = "sg-xxxxxxxx"
}

variable "msk_cluster_arn" {
  type    = string
  default = "arn:aws:kafka:ap-southeast-1:<AWS_ACCOUNT_ID>:cluster/your-msk-cluster/00000000-0000-0000-0000-000000000000-1"
}

variable "kafka_brokers" {
  type    = string
  default = "b-1.your-msk.kafka.ap-southeast-1.amazonaws.com:9098,b-2.your-msk.kafka.ap-southeast-1.amazonaws.com:9098"
}

# --- compute ---
variable "instance_type" {
  type    = string
  default = "t3.large"
}

variable "secret_name" {
  type    = string
  default = "acme-ofv/dev"
}
