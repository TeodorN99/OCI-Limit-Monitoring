output "topic" {
  value = module.notifications.topic
}

output "apps" {
  value = module.functions.apps
}

output "project_compartment_id" {
  value = local.compartment_ids[var.project_compartment_key]
}

output "network" {
  value = var.create_network ? {
    vcn_id     = module.network[0].vcn_id
    subnet_ids = module.network[0].subnet_ids
  } : null
}
