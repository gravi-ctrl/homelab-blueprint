terraform {
  required_providers {
    github = {
      source  = "integrations/github"
      version = "6.13.0"
    }
  }

  backend "s3" {
    bucket = "gravi-ctrl-tf-state"
    key    = "github.tfstate/terraform.tfstate" # The folder & filename in B2
    region = "us-east-1"

    endpoints = {
      s3 = "https://s3.eu-central-003.backblazeb2.com"
    }
    use_path_style              = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_metadata_api_check     = true
    skip_requesting_account_id  = true
    skip_s3_checksum            = true
  }
}

provider "github" {}
