output "topic" {
  value = module.notifications.topic
}

output "apps" {
  value = module.functions.apps
}

output "function_invocation_log_groups" {
  value = module.functions.invocation_log_groups
}

output "function_invocation_logs" {
  value = module.functions.invocation_logs
}

output "project_compartment_id" {
  value = local.compartment_ids[var.project_compartment_key]
}

output "network" {
  value = var.create_network ? {
    vcn_id             = module.network[0].vcn_id
    subnet_ids         = module.network[0].subnet_ids
    service_gateway_id = module.network[0].service_gateway_id
    nat_gateway_id     = module.network[0].nat_gateway_id
  } : null
}
