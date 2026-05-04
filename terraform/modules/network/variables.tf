// Copyright (c) 2020, Oracle and/or its affiliates. All rights reserved.
// Licensed under the Universal Permissive License v 1.0 as shown at https://oss.oracle.com/licenses/upl.

variable "compartment_id" {
  type = string
}

variable "vcn_name" {
  type = string
}

variable "vcn_cidr" {
  type = string
}

variable "vcn_dns_label" {
  type = string
}

variable "functions_subnet_name" {
  type = string
}

variable "functions_subnet_cidr" {
  type = string
}

variable "functions_subnet_dns_label" {
  type = string
}
