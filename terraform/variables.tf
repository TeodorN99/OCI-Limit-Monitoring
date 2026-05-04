// Copyright (c) 2020, Oracle and/or its affiliates. All rights reserved.
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.

variable "provider_oci" {
  type = object({
    tenancy             = string
    region              = string
    config_file_path    = string
    config_file_profile = string
    auth                = optional(string, "ApiKey")
  })
}

variable "compartment_ids" {
  type    = map(string)
  default = {}
}

variable "create_project_compartment" {
  type    = bool
  default = true
}

variable "project_compartment_key" {
  type    = string
  default = "limit_monitoring"
}

variable "project_compartment_name" {
  type    = string
  default = "oci-limit-monitoring"
}

variable "project_compartment_description" {
  type    = string
  default = "Resources for OCI limit monitoring."
}

variable "subnet_ids" {
  type    = map(string)
  default = {}
}

variable "create_network" {
  type    = bool
  default = true
}

variable "network_compartment_key" {
  type    = string
  default = "limit_monitoring"
}

variable "network" {
  type = object({
    vcn_name                   = string
    vcn_cidr                   = string
    vcn_dns_label              = string
    functions_subnet_name      = string
    functions_subnet_cidr      = string
    functions_subnet_dns_label = string
  })

  default = {
    vcn_name                   = "limit-monitoring-vcn"
    vcn_cidr                   = "10.42.0.0/16"
    vcn_dns_label              = "limitmon"
    functions_subnet_name      = "functions-private-subnet"
    functions_subnet_cidr      = "10.42.1.0/24"
    functions_subnet_dns_label = "fnprivate"
  }
}

variable "app_params" {
  type = map(object({
    compartment_name = string
    subnet_name      = list(string)
    display_name     = string
    config           = map(string)
    freeform_tags    = map(string)
  }))
}

variable "topic_params" {
  type = map(object({
    comp_name   = string
    topic_name  = string
    description = string
  }))
}

variable "subscription_params" {
  type = map(object({
    comp_name  = string
    endpoint   = string
    protocol   = string
    topic_name = string
  }))
}

variable "function_dynamic_group_name" {
  type    = string
  default = "limit_monitoring_functions_dg"
}

variable "function_policy_name" {
  type    = string
  default = "limit_monitoring_functions_policy"
}
