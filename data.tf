resource "aws_dynamodb_table" "rolodex" {
  name         = var.project
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  ttl { # container heartbeats expire on their own
    attribute_name = "ttl"
    enabled        = true
  }
}

# The owner key: generated here, stored encrypted, injected into the containers by ECS at start.
# Read it with:  aws ssm get-parameter --name /rolodex/owner-key --with-decryption --query Parameter.Value --output text
resource "random_password" "owner_key" {
  length  = 24
  special = false
}

resource "aws_ssm_parameter" "owner_key" {
  name  = "/${var.project}/owner-key"
  type  = "SecureString"
  value = random_password.owner_key.result
}

# Shared secret between CloudFront and the load balancer, so only *this* distribution can reach it.
resource "random_password" "origin_secret" {
  length  = 32
  special = false
}
