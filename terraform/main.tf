// Copyright (c) 2020, Oracle and/or its affiliates. All rights reserved.
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.

data "oci_identity_regions" "this" {
}

data "oci_identity_tenancy" "this" {
  tenancy_id = var.provider_oci.tenancy
}

locals {
  region_map = { for region in data.oci_identity_regions.this.regions : region.key => region.name }
  compartment_ids = merge(
    var.compartment_ids,
    {
      (var.project_compartment_key) = var.create_project_compartment ? oci_identity_compartment.project[0].id : var.compartment_ids[var.project_compartment_key]
    }
  )
  subnet_ids = merge(
    var.subnet_ids,
    var.create_network ? module.network[0].subnet_ids : {}
  )
}

provider "oci" {
  auth                = var.provider_oci.auth
  tenancy_ocid        = var.provider_oci.tenancy
  config_file_profile = var.provider_oci.auth == "ApiKey" ? var.provider_oci.config_file_profile : null
  region              = var.provider_oci.region
}

provider "oci" {
  alias               = "home"
  auth                = var.provider_oci.auth
  tenancy_ocid        = var.provider_oci.tenancy
  config_file_profile = var.provider_oci.auth == "ApiKey" ? var.provider_oci.config_file_profile : null
  region              = lookup(local.region_map, data.oci_identity_tenancy.this.home_region_key)
}

resource "oci_identity_compartment" "project" {
  count          = var.create_project_compartment ? 1 : 0
  compartment_id = var.provider_oci.tenancy
  description    = var.project_compartment_description
  enable_delete  = true
  name           = var.project_compartment_name
}

module "notifications" {
  providers           = { oci = oci.home }
  source              = "./modules/notifications"
  topic_params        = var.topic_params
  subscription_params = var.subscription_params
  compartment_ids     = local.compartment_ids
}

module "network" {
  count     = var.create_network ? 1 : 0
  providers = { oci = oci.home }
  source    = "./modules/network"

  compartment_id             = local.compartment_ids[var.network_compartment_key]
  enable_nat_gateway         = var.enable_private_nat_egress
  vcn_name                   = var.network.vcn_name
  vcn_cidr                   = var.network.vcn_cidr
  vcn_dns_label              = var.network.vcn_dns_label
  functions_subnet_name      = var.network.functions_subnet_name
  functions_subnet_cidr      = var.network.functions_subnet_cidr
  functions_subnet_dns_label = var.network.functions_subnet_dns_label
}

module "functions" {
  providers                         = { oci = oci.home }
  source                            = "./modules/functions"
  compartment_ids                   = local.compartment_ids
  subnet_ids                        = local.subnet_ids
  app_params                        = var.app_params
  enable_invocation_logs            = var.enable_function_invocation_logs
  invocation_log_retention_duration = var.function_invocation_log_retention_duration
}

resource "oci_identity_dynamic_group" "functions" {
  compartment_id = var.provider_oci.tenancy
  description    = "Limit monitoring functions running with OCI resource principal auth."
  matching_rule  = "ALL {resource.type = 'fnfunc', resource.compartment.id = '${local.compartment_ids[var.project_compartment_key]}'}"
  name           = var.function_dynamic_group_name
}

resource "oci_identity_policy" "functions" {
  compartment_id = var.provider_oci.tenancy
  description    = "Allows the limit monitoring function to inspect limits and publish notifications."
  name           = var.function_policy_name

  statements = [
    "Allow dynamic-group ${oci_identity_dynamic_group.functions.name} to inspect tenancies in tenancy",
    "Allow dynamic-group ${oci_identity_dynamic_group.functions.name} to inspect compartments in tenancy",
    "Allow dynamic-group ${oci_identity_dynamic_group.functions.name} to inspect limits in tenancy",
    "Allow dynamic-group ${oci_identity_dynamic_group.functions.name} to inspect resource-availability in tenancy",
    "Allow dynamic-group ${oci_identity_dynamic_group.functions.name} to read resource-availability in tenancy",
    "Allow dynamic-group ${oci_identity_dynamic_group.functions.name} to use ons-topics in tenancy"
  ]
}
