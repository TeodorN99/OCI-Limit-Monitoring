// Copyright (c) 2020, Oracle and/or its affiliates. All rights reserved.
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.

provider_oci = {
  tenancy             = "ocid1.tenancy.oc1.."
  region              = "eu-amsterdam-1"
  config_file_path    = "~/.oci/config"
  config_file_profile = "DEFAULT"
  auth                = "ApiKey"
}

create_project_compartment      = true
project_compartment_key         = "limit_monitoring"
project_compartment_name        = "oci-limit-monitoring"
project_compartment_description = "Resources for OCI limit monitoring."

compartment_ids = {}

create_network          = true
network_compartment_key = "limit_monitoring"
network = {
  vcn_name                   = "limit-monitoring-vcn"
  vcn_cidr                   = "10.42.0.0/16"
  vcn_dns_label              = "limitmon"
  functions_subnet_name      = "functions-private-subnet"
  functions_subnet_cidr      = "10.42.1.0/24"
  functions_subnet_dns_label = "fnprivate"
}

app_params = {
  limit_monitoring = {
    compartment_name = "limit_monitoring"
    subnet_name      = ["functions"]
    display_name     = "limit-monitoring-app"
    config           = {}
    freeform_tags    = {}
  }
}

topic_params = {
  "limit-monitoring-topic" = {
    comp_name   = "limit_monitoring"
    topic_name  = "limit-monitoring-topic"
    description = "OCI limit monitoring alerts"
  }
}

subscription_params = {
  email = {
    comp_name  = "limit_monitoring"
    endpoint   = "replace-me@example.com"
    protocol   = "EMAIL"
    topic_name = "limit-monitoring-topic"
  }
}
