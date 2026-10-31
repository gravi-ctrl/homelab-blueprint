resource "github_repository" "this" {
  name = var.repo_name
  description = var.repo_desc
  homepage_url = var.homepage_url
  visibility = var.visib
  has_issues = var.has_issues
  has_discussions = var.has_discussions
  has_projects = var.has_projects
  has_wiki = var.has_wiki
  topics = var.topics 
  delete_branch_on_merge = var.delete_on_merge
  allow_merge_commit = var.allow_merge_commit
  allow_squash_merge = var.allow_squash_merge
  allow_rebase_merge = var.rebase_merge

  lifecycle {
    prevent_destroy = true
  }

}

resource "github_repository_vulnerability_alerts" "this" {
  repository = github_repository.this.name
  enabled = var.vuln_alerts
}
   
