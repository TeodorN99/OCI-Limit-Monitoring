output "vcn_id" {
  value = oci_core_vcn.this.id
}

output "subnet_ids" {
  value = {
    functions = oci_core_subnet.functions.id
  }
}

output "service_gateway_id" {
  value = oci_core_service_gateway.this.id
}
