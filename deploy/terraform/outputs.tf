output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "api_url" {
  value = "https://${aws_lb.main.dns_name}"
}

output "mock_url" {
  value = "https://${aws_lb.main.dns_name}:8100"
}

output "instance_id" {
  value = aws_instance.app.id
}

output "instance_public_ip" {
  value = aws_instance.app.public_ip
}

output "deploy_bucket" {
  value = aws_s3_bucket.deploy.id
}
