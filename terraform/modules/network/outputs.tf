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

output "nat_gateway_id" {
  value = try(oci_core_nat_gateway.this[0].id, null)
}
