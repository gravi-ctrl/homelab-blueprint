variable "repo_name" {
  description = "The repo's name"
  type = string
}

variable "homepage_url" {
  description = "The URL of the repository homepage"
  type        = string
  default     = null
}

variable "visib" {
  description = "Public or private"
  type = string
  default = "private"
}

variable "repo_desc" {
  description = "The about section in the repo"
  type = string
  default = null
}

variable "delete_on_merge" {
  description = "Automatically deletes the head branch after a PR is merged"
  type = bool
  default = true
}

variable "allow_squash_merge" {
  description = "Best way to merge PRS into 'main' (on tidy commit)"
  type = bool
  default = true
}

variable "allow_merge_commit" {
  description = "Stops 'merge branch feature into main' bubble commit from polluting the log"
  type = bool
  default = false
}

variable "has_issues" {
  description = "Show issues page"
  type = bool
  default = true
}

variable "has_projects" {
  description = "Show projects tab"
  type = bool
  default = false
}

variable "has_wiki" {
  description = "Shows the wiki tab"
  type = bool
  default = false
}

variable "has_discussions" {
  description = "Shows discussions tab"
  type = bool
  default = false
}

variable "vuln_alerts" {
  description = "Toggles GH dependabot security alerts for vulnerable packages"
  type = bool
  default = true
}

variable "topics" {
  description = "Tags that appear on repo"
  type = list(string)
  default = []
}

variable "rebase_merge" {
  description = "Whether you want rebase merges enabled"
  type = bool
  default = true
}
