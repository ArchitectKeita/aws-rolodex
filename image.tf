# The container image is built in AWS by CodeBuild, so deploying needs no Docker on your machine.
# Every code change gets its own image tag (a hash of the app folder): images are never overwritten,
# and rolling back is pointing the service at an older tag.

resource "aws_ecr_repository" "app" {
  name                 = var.project
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true # basic vulnerability scan on every push
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1, description = "keep the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

data "archive_file" "app" {
  type        = "zip"
  source_dir  = "${path.module}/app"
  output_path = "${path.module}/build/app.zip"
  excludes    = ["__pycache__", "**/__pycache__/**"]
}

locals {
  image_tag = substr(data.archive_file.app.output_md5, 0, 12)
  image     = "${aws_ecr_repository.app.repository_url}:${local.image_tag}"
}

resource "random_id" "src" {
  byte_length = 4
}

resource "aws_s3_bucket" "src" {
  bucket        = "${var.project}-build-src-${random_id.src.hex}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "src" {
  bucket                  = aws_s3_bucket.src.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_object" "src" {
  bucket = aws_s3_bucket.src.id
  key    = "app.zip"
  source = data.archive_file.app.output_path
  etag   = data.archive_file.app.output_md5
}

resource "aws_cloudwatch_log_group" "build" {
  name              = "/aws/codebuild/${var.project}-image"
  retention_in_days = 7
}

data "aws_iam_policy_document" "codebuild_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["codebuild.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "build" {
  name               = "${var.project}-image-build"
  assume_role_policy = data.aws_iam_policy_document.codebuild_trust.json
}

resource "aws_iam_role_policy" "build" {
  name = "build"
  role = aws_iam_role.build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "ReadSource", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.src.arn}/*" },
      { Sid = "Logs", Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.build.arn}:*" },
      { Sid = "EcrLogin", Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Sid = "PushThisRepoOnly", Effect = "Allow", Resource = aws_ecr_repository.app.arn, Action = [
        "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:DescribeImages"] },
    ]
  })
}

resource "aws_codebuild_project" "image" {
  name          = "${var.project}-image"
  description   = "Builds the Rolodex container and pushes it to ECR"
  service_role  = aws_iam_role.build.arn
  build_timeout = 15

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.src.bucket}/${aws_s3_object.src.key}"
    buildspec = <<-YAML
      version: 0.2
      phases:
        pre_build:
          commands:
            - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $${REPO_URI%%/*}
            - |
              if aws ecr describe-images --repository-name $REPO_NAME --image-ids imageTag=$IMAGE_TAG >/dev/null 2>&1; then
                echo "image $IMAGE_TAG already exists; nothing to build"; echo SKIP > /tmp/skip
              fi
        build:
          commands:
            - '[ -f /tmp/skip ] || docker build --pull -t $REPO_URI:$IMAGE_TAG .'
        post_build:
          commands:
            - '[ -f /tmp/skip ] || docker push $REPO_URI:$IMAGE_TAG'
            - echo "pushed $REPO_URI:$IMAGE_TAG"
    YAML
  }

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type            = "LINUX_CONTAINER"
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/amazonlinux2-x86_64-standard:5.0"
    privileged_mode = true # needed to run docker build

    environment_variable {
      name  = "REPO_URI"
      value = aws_ecr_repository.app.repository_url
    }
    environment_variable {
      name  = "REPO_NAME"
      value = aws_ecr_repository.app.name
    }
    environment_variable {
      name  = "IMAGE_TAG"
      value = local.image_tag
    }
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.build.name
    }
  }
}

# Kick off a build whenever the app's code changes. Waiting for the load balancer first gives IAM time
# to finish propagating the brand-new build role (the race that bit Project 06).
resource "terraform_data" "build" {
  triggers_replace = [local.image_tag]

  provisioner "local-exec" {
    command = "aws codebuild start-build --project-name ${aws_codebuild_project.image.name} --region ${var.region} --query build.id --output text"
  }

  depends_on = [aws_s3_object.src, aws_iam_role_policy.build, aws_lb.app]
}
