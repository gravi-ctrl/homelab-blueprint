output "homelab_ssh_clone_url" {
  description = "SSH clone from the repo"
  value       = module.homelab.ssh_clone_url
}

output "stacks_ssh_clone_url" {
  description = "SSH clone from the repo"
  value       = module.stacks.ssh_clone_url
}

output "ctrl-s-master_ssh_clone_url" {
  description = "SSH clone from the repo"
  value       = module.ctrl_s_master.ssh_clone_url
}
