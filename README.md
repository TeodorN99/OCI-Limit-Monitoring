# OCI Limit Monitoring

This project deploys one OCI Function in the tenancy home region. The function checks service limits across all subscribed OCI regions and publishes a notification when available capacity drops below the configured threshold.

Scheduling is handled by OCI Resource Scheduler. The old Object Storage bucket, lifecycle policy, delete event, main function, and per-region worker functions have been removed.

## Architecture

```text
OCI Resource Scheduler
  -> invokes one OCI Function in detached mode
  -> function loops over subscribed regions
  -> function checks OCI service limits and availability
  -> function publishes alerts to OCI Notifications
```

The function runs in the home region, but creates regional OCI SDK clients while it checks each subscribed region.

## What Terraform Creates

- Optional project compartment.
- Private VCN, private regional subnet, service gateway, route table, and security list for OCI Functions.
- OCI Functions application in the tenancy home region.
- OCI Notifications topic and subscription.
- Dynamic group and policy for the function resource principal.

Terraform does not deploy the function image. The deployment script uses the Fn CLI for that, then creates or updates the OCI Resource Scheduler schedule once the function OCID exists.

## Current Dependency Pins

- Terraform OCI provider: `oracle/oci >= 8.12.0, < 9.0.0`
- Function Python dependencies:
  - `fdk==0.1.109`
  - `oci==2.171.0`
  - `backoff==2.2.1`
- Deployment script dependencies:
  - `oci==2.171.0`
  - `Jinja2==3.1.6`

## Prerequisites

- Terraform `>= 1.3.0`
- Docker
- Fn CLI
- OCI CLI/SDK config profile. A sample profile-based variable file is provided for `SOFIANE`.
- IAM permissions to manage compartments, Functions, Notifications, dynamic groups, policies, and Resource Scheduler schedules.
- An OCIR auth token for container registry login, unless the deployment environment is already authenticated to push to OCIR.

## Configure Terraform

Create your local variables file from the example, then edit it:

```bash
cp terraform/sofiane.tfvars.example terraform/sofiane.tfvars
```

Edit `terraform/sofiane.tfvars`:

```hcl
provider_oci = {
  tenancy             = "ocid1.tenancy.oc1..replace-with-your-tenancy-ocid"
  region              = "eu-amsterdam-1"
  config_file_profile = "SOFIANE"
  auth                = "ApiKey"
}

create_network = true
network = {
  vcn_name                   = "limit-monitoring-vcn"
  vcn_cidr                   = "10.42.0.0/16"
  vcn_dns_label              = "limitmon"
  functions_subnet_name      = "functions-private-subnet"
  functions_subnet_cidr      = "10.42.1.0/24"
  functions_subnet_dns_label = "fnprivate"
}

subscription_params = {
  email = {
    comp_name  = "limit_monitoring"
    endpoint   = "replace-with-your-email@example.com"
    protocol   = "EMAIL"
    topic_name = "limit-monitoring-topic"
  }
}
```

By default, this creates a compartment named `oci-limit-monitoring`. To reuse an existing compartment, set:

```hcl
create_project_compartment = false

compartment_ids = {
  limit_monitoring = "ocid1.compartment.oc1..existing-compartment-ocid"
}
```

By default, Terraform also creates a private VCN for Functions. It does not create a public subnet or internet gateway. The private subnet routes Oracle service traffic through a service gateway to `All <region> Services In Oracle Services Network`, which lets the function reach OCI APIs and OCIR without public internet exposure.

To reuse an existing private subnet instead, set:

```hcl
create_network = false

subnet_ids = {
  functions = "ocid1.subnet.oc1..existing-private-subnet-ocid"
}
```

Apply Terraform:

```bash
cd terraform
terraform init -upgrade
terraform apply -var-file=sofiane.tfvars
```

In OCI Cloud Shell, use instance principal auth instead of a local OCI config file:

```hcl
provider_oci = {
  tenancy             = "ocid1.tenancy.oc1..replace-with-your-tenancy-ocid"
  region              = "eu-frankfurt-1"
  config_file_path    = ""
  config_file_profile = ""
  auth                = "InstancePrincipal"
}
```

Save these outputs:

- `project_compartment_id`
- `apps["limit-monitoring-app"]`
- `topic["limit-monitoring-topic"]`

## Deploy the Function and Schedule

Install deployment dependencies:

```bash
python -m pip install -r serverless/deployment/requirements.txt
```

Deploy the single all-regions function and create/update the Resource Scheduler schedule.

From OCI Cloud Shell, use the Cloud Shell Fn provider. Cloud Shell has the Fn CLI installed, and typically has `podman` available for image builds:

```bash
cd serverless/deployment
python3 deployment.py \
  -auth instance_principal \
  -region eu-frankfurt-1 \
  -tenancy_id '<tenancy_ocid>' \
  -profile DEFAULT \
  -fn_provider oracle-cs \
  -container_cli auto \
  -user '<tenancy_namespace>/<user_email>' \
  -password '<ocir_auth_token>' \
  -compartment_id '<project_compartment_id>' \
  -app_name 'limit-monitoring-app' \
  -topic_id '<topic_ocid>' \
  -percentage 90
```

Oracle's Cloud Shell Functions quickstart still tells you to generate an auth token and log in to OCIR before deploying. So in most tenancies, you should expect to provide `-user` and `-password` even from Cloud Shell.

If Cloud Shell is already authenticated to push to OCIR, omit `-user` and `-password` and add:

```bash
-skip_docker_login
```

From a local machine, use the local Fn provider:

```bash
cd serverless/deployment
python deployment.py \
  -auth api_key \
  -profile SOFIANELS \
  -config_file 'C:/Users/Sofiane Mahdjoubi/.oci/config' \
  -fn_provider oracle \
  -user '<tenancy_namespace>/<user_email>' \
  -password '<ocir_auth_token>' \
  -compartment_id '<project_compartment_id>' \
  -app_name 'limit-monitoring-app' \
  -topic_id '<topic_ocid>' \
  -percentage 90
```

The default schedule is every Monday at `07:00 UTC`:

```text
0 7 * * 1
```

Override it with:

```bash
-recurrence_type CRON -recurrence_details '0 7 * * 1'
```

OCI Resource Scheduler uses UTC. The schedule invokes the function with the `START_RESOURCE` action, which is how scheduled OCI Functions are started.

## Optional Filters

Check only specific regions:

```bash
-regions 'eu-amsterdam-1,us-ashburn-1'
```

Check only specific OCI services:

```bash
-services 'compute,block-storage'
```

If `regions` is omitted, all subscribed regions are checked.

## Function Configuration

The deployment script writes [serverless/fn/func.yaml](./serverless/fn/func.yaml) before deploying. The important config values are:

- `percentage`: Alert threshold.
- `topic_id`: OCI Notifications topic OCID.
- `regions`: Optional comma-separated region allowlist.
- `services`: Optional comma-separated service allowlist.

## Notes

- Email subscriptions must be confirmed before alerts are delivered.
- Resource Scheduler requires a dynamic group containing the schedule resource and a policy allowing that dynamic group to manage Functions. The deployment script creates or updates those resources after the schedule exists.
- The function resource principal policy is created by Terraform and allows the function to inspect limits/resource availability and publish to Notifications.
