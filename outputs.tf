output "rolodex_url" {
  description = "The app, over HTTPS"
  value       = "https://${aws_cloudfront_distribution.app.domain_name}"
}

output "owner_key_command" {
  description = "Run this to see the owner key"
  value       = "aws ssm get-parameter --name ${aws_ssm_parameter.owner_key.name} --with-decryption --query Parameter.Value --output text"
}

output "image" {
  value = local.image
}

output "load_balancer_direct" {
  description = "Try it: going around CloudFront gets a 403"
  value       = "http://${aws_lb.app.dns_name}"
}
