// Copyright (c) 2020, Oracle and/or its affiliates. All rights reserved.
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.

terraform {
  required_providers {
    oci = {
      source                = "oracle/oci"
      configuration_aliases = []
    }
  }
}

data "oci_core_services" "oracle_services" {
  filter {
    name   = "name"
    regex  = true
    values = ["All .* Services In Oracle Services Network"]
  }
}

locals {
  oracle_services_cidr = data.oci_core_services.oracle_services.services[0].cidr_block
  oracle_services_id   = data.oci_core_services.oracle_services.services[0].id
}

resource "oci_core_vcn" "this" {
  cidr_block     = var.vcn_cidr
  compartment_id = var.compartment_id
  display_name   = var.vcn_name
  dns_label      = var.vcn_dns_label
}

resource "oci_core_service_gateway" "this" {
  compartment_id = var.compartment_id
  display_name   = "${var.vcn_name}-service-gateway"
  vcn_id         = oci_core_vcn.this.id

  services {
    service_id = local.oracle_services_id
  }
}

resource "oci_core_route_table" "private" {
  compartment_id = var.compartment_id
  display_name   = "${var.vcn_name}-private-routes"
  vcn_id         = oci_core_vcn.this.id

  route_rules {
    destination       = local.oracle_services_cidr
    destination_type  = "SERVICE_CIDR_BLOCK"
    network_entity_id = oci_core_service_gateway.this.id
  }
}

resource "oci_core_security_list" "functions" {
  compartment_id = var.compartment_id
  display_name   = "${var.vcn_name}-functions-security-list"
  vcn_id         = oci_core_vcn.this.id

  egress_security_rules {
    description      = "Allow private egress to Oracle Services Network for OCI APIs and OCIR."
    destination      = local.oracle_services_cidr
    destination_type = "SERVICE_CIDR_BLOCK"
    protocol         = "all"
    stateless        = true
  }

  ingress_security_rules {
    description = "Allow return traffic from Oracle Services Network for stateless function egress."
    protocol    = "all"
    source      = local.oracle_services_cidr
    source_type = "SERVICE_CIDR_BLOCK"
    stateless   = true
  }
}

resource "oci_core_subnet" "functions" {
  cidr_block                 = var.functions_subnet_cidr
  compartment_id             = var.compartment_id
  display_name               = var.functions_subnet_name
  dns_label                  = var.functions_subnet_dns_label
  prohibit_public_ip_on_vnic = true
  route_table_id             = oci_core_route_table.private.id
  security_list_ids          = [oci_core_security_list.functions.id]
  vcn_id                     = oci_core_vcn.this.id
}
