terraform {
  required_version = ">= 1.5"

  backend "s3" {
    bucket         = "<YOUR_STATE_BUCKET>"
    key            = "eks/terraform.tfstate"
    region         = "<AWS_REGION>"
    dynamodb_table = "terraform-lock"
    encrypt        = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "agentic-rag"
      ManagedBy = "terraform"
    }
  }
}
