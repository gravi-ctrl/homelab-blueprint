module "homelab" {
  source       = "./modules/github_repo"
  repo_name    = "homelab-blueprint"
  visib        = "public"
  repo_desc    = "The 'Brain' of the homelab: Infrastructure-as-Code for system setup, security hardening, and disaster recovery."
  homepage_url = "https://gravi-ctrl.github.io/homelab-blueprint/"
  topics = [
    "automation",
    "terraform",
    "bash",
    "docker",
    "homelab",
    "infrastructure-as-code",
    "linux",
    "python",
    "self-hosted",
  ]
}

module "stacks" {
  source       = "./modules/github_repo"
  repo_name    = "server-docker-backup"
  visib        = "public"
  repo_desc    = "A backup of all of my docker container configs in /opt/stacks."
  homepage_url = "https://gravi-ctrl.github.io/server-docker-backup/"
}

module "ctrl_s_master" {
  source    = "./modules/github_repo"
  repo_name = "ctrl-s-master"
  visib     = "public"
  repo_desc = "Automated, secure local archiving for critical cloud data. Zero-disk credential footprint via encrypted RAM-loading and automated vault management."
  topics = [
    "api",
    "automation",
    "bitwarden",
    "encryption",
    "linux",
    "python",
    "security",
    "veracrypt",
    "windows",
  ]
}
