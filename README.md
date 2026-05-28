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
- Private VCN, private regional subnet, service gateway, NAT gateway, route table, and security list for OCI Functions.
- OCI Functions application in the tenancy home region.
- OCI Logging log group and Function Invocation Logs for the Functions application.
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

- OCI Cloud Shell is recommended. It already includes Terraform, OCI CLI, Fn CLI, and a container engine.
- IAM permissions to manage compartments, Functions, Notifications, dynamic groups, policies, and Resource Scheduler schedules.
- An OCIR auth token for container registry login.

For local deployment instead of Cloud Shell, you also need Terraform `>= 1.3.0`, Docker or Podman, Fn CLI, and an OCI SDK config profile.

## Recommended Cloud Shell Deployment

Clone the branch:

```bash
git clone -b modernize-resource-scheduler <your-repo-url>
cd OCI-Limit-Monitoring
```

Install the Python deployment dependencies:

```bash
python3 -m pip install -r serverless/deployment/requirements.txt --user
```

Create and edit your local Terraform variables file:

```bash
cp terraform/sofiane.tfvars.example terraform/sofiane.tfvars
vi terraform/sofiane.tfvars
```

In Cloud Shell, use Cloud Shell auth in `terraform/sofiane.tfvars`:

```hcl
provider_oci = {
  tenancy             = "ocid1.tenancy.oc1..replace-with-your-tenancy-ocid"
  region              = "eu-paris-1"
  config_file_path    = ""
  config_file_profile = ""
  auth                = "InstancePrincipal"
}
```

Deploy the Terraform resources:

```bash
cd terraform
terraform init -upgrade
terraform plan -var-file sofiane.tfvars -out tfplan
terraform apply tfplan
```

Save the Terraform outputs:

```bash
terraform output project_compartment_id
terraform output topic
terraform output apps
terraform output function_invocation_logs
```

Confirm the OCI Notifications email subscription before expecting alert emails.

Deploy the function image and Resource Scheduler schedule:

```bash
cd ../serverless/deployment

python3 deployment.py \
  -tenancy_id '<tenancy_ocid>' \
  -auth cloud_shell \
  -region eu-paris-1 \
  -fn_provider oracle-cs \
  -container_cli auto \
  -user '<tenancy_namespace>/<user_email>' \
  -compartment_id '<project_compartment_id>' \
  -app_name 'limit-monitoring-app' \
  -topic_id '<topic_ocid>' \
  -percentage 90 \
  -services 'compute,block-storage,vcn,load-balancer,database' \
  -max_workers 8
```

To customize when the function runs, add `-recurrence_type` and `-recurrence_details` to the deployment command. For example, daily at `07:00 UTC`:

```bash
-recurrence_type CRON -recurrence_details '0 7 * * *'
```

When `-password` is omitted, the script prompts for the OCIR auth token without echoing it.

After deployment, remove any saved OCIR registry credentials from Cloud Shell if you do not want the container engine to keep them on disk:

```bash
docker logout <ocir_registry_host> 2>/dev/null || true
podman logout <ocir_registry_host> 2>/dev/null || true
unset OCIR_TOKEN
```

This removes the local Docker or Podman login for the registry host used by the deployment script, such as `ocir.eu-paris-1.oci.oraclecloud.com` in OC1 or `ocir.eu-frankfurt-2.oci.oraclecloud.eu` in OC19.

For example, the OCIR user format is:

```text
<tenancy_namespace>/<user_email>
```

Run a manual test:

```bash
fn invoke limit-monitoring-app limit-monitoring
```

Query recent invocation logs after a test run:

```bash
oci logging-search search-logs \
  --search-query "search '<project_compartment_id>/<function_invocation_log_ocid>' | sort by datetime desc" \
  --time-start "$(date -u -d '-30 minutes' +%Y-%m-%dT%H:%M:%SZ)" \
  --time-end "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

## Configure Terraform

This section is useful for local deployments or for adjusting the Terraform variables beyond the Cloud Shell quick path.

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

create_network            = true
enable_private_nat_egress = true
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

By default, Terraform also creates a private VCN for Functions. It does not create a public subnet or internet gateway. The private subnet routes same-region Oracle service traffic through a service gateway to `All <region> Services In Oracle Services Network`.

Because the function can check every subscribed OCI region from one home-region function, Terraform also creates a NAT gateway by default. This keeps the subnet private while allowing outbound HTTPS to cross-region OCI API endpoints such as `limits.eu-frankfurt-1.oci.oraclecloud.com` in OC1 or `limits.eu-frankfurt-2.oci.oraclecloud.eu` in OC19.

If you only check the function home region and do not need outbound public HTTPS, you can disable NAT creation:

```hcl
enable_private_nat_egress = false
```

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
terraform plan -var-file sofiane.tfvars -out tfplan
terraform apply tfplan
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
- `function_invocation_logs["limit-monitoring-app"]`

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
  -auth cloud_shell \
  -region eu-frankfurt-1 \
  -tenancy_id '<tenancy_ocid>' \
  -profile DEFAULT \
  -fn_provider oracle-cs \
  -container_cli auto \
  -user '<tenancy_namespace>/<user_email>' \
  -compartment_id '<project_compartment_id>' \
  -app_name 'limit-monitoring-app' \
  -topic_id '<topic_ocid>' \
  -percentage 90 \
  -services 'compute,block-storage,vcn,load-balancer,database' \
  -max_workers 8
```

The deployment script derives the Functions API URL and OCIR registry host from the home region's realm. For example, OC1 Frankfurt uses `https://functions.eu-frankfurt-1.oci.oraclecloud.com` and `ocir.eu-frankfurt-1.oci.oraclecloud.com`, while EU Sovereign Cloud Frankfurt uses `https://functions.eu-frankfurt-2.oci.oraclecloud.eu` and `ocir.eu-frankfurt-2.oci.oraclecloud.eu`.

To customize when the function runs, add `-recurrence_type` and `-recurrence_details` to the deployment command. For example, daily at `07:00 UTC`:

```bash
-recurrence_type CRON -recurrence_details '0 7 * * *'
```

Oracle's Cloud Shell Functions quickstart still tells you to generate an auth token and log in to OCIR before deploying. So in most tenancies, you should expect to provide `-user` and enter the OCIR auth token at the hidden prompt even from Cloud Shell.

For non-interactive use, set the token in an environment variable and keep `-password` off the command line:

```bash
read -s OCIR_TOKEN
export OCIR_TOKEN
```

The deployment script reads `OCIR_TOKEN` by default when `-password` is omitted. Use `-password_env NAME` to choose a different environment variable.

After deployment, remove any saved OCIR registry credentials from Cloud Shell if you do not want the container engine to keep them on disk:

```bash
docker logout <ocir_registry_host> 2>/dev/null || true
podman logout <ocir_registry_host> 2>/dev/null || true
unset OCIR_TOKEN
```

This removes the local Docker or Podman login for the registry host used by the deployment script.

If Cloud Shell is already authenticated to push to OCIR, omit `-user` and add:

```bash
-skip_docker_login
```

## OCI Sovereign Cloud and Non-OC1 Realms

The runtime function and deployment script are realm-aware when the OCI SDK and Terraform provider know the target region. For Oracle EU Sovereign Cloud, use an OC19 tenancy and one of the OC19 region identifiers, for example `eu-frankfurt-2` or `eu-madrid-2`. An OC1 tenancy such as `ocid1.tenancy.oc1..` cannot be pointed at OC19 regions.

For EU Sovereign Cloud endpoints, OCI uses the pattern:

```text
https://<service_api_name>.<region>.oci.oraclecloud.eu
```

For example:

```text
https://limits.eu-frankfurt-2.oci.oraclecloud.eu
https://functions.eu-frankfurt-2.oci.oraclecloud.eu
ocir.eu-frankfurt-2.oci.oraclecloud.eu/<tenancy_namespace>/limits
```

If you target a newly released or private realm region that your local OCI SDK, OCI CLI, Fn CLI, or Terraform provider does not recognize yet, add region metadata with `OCI_REGION_METADATA` or `~/.oci/regions-config.json`, or upgrade the relevant toolchain.

From a local machine, use the local Fn provider:

```bash
cd serverless/deployment
python deployment.py \
  -auth api_key \
  -profile SOFIANELS \
  -config_file 'C:/Users/Sofiane Mahdjoubi/.oci/config' \
  -fn_provider oracle \
  -user '<tenancy_namespace>/<user_email>' \
  -compartment_id '<project_compartment_id>' \
  -app_name 'limit-monitoring-app' \
  -topic_id '<topic_ocid>' \
  -percentage 90 \
  -services 'compute,block-storage,vcn,load-balancer,database' \
  -max_workers 8
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

Check only specific limit names for one or more services:

```bash
-limit_names 'compute:standard-e4-core-count|standard-e5-core-count;block-storage:total-storage-gb|volume-count'
```

`limit_names` is scoped per OCI Limits service name. If a service has a `limit_names` entry, only those limits are checked for that service. Selected services without a `limit_names` entry still scan all supported limits for that service.

To check only a few Compute limits, combine both filters:

```bash
-services 'compute' -limit_names 'compute:standard-e4-core-count|standard-e5-core-count'
```

If `services` is omitted, the function uses this default allowlist:

```text
compute,block-storage,vcn,load-balancer,database
```

Use the OCI Limits programmatic service names exactly as returned by `oci limits service list`.

To scan every service, use:

```bash
-services 'all'
```

Control the number of concurrent `GetResourceAvailability` calls:

```bash
-max_workers 8
```

If `regions` is omitted, all subscribed regions are checked.

## Function Configuration

The deployment script writes [serverless/fn/func.yaml](./serverless/fn/func.yaml) before deploying. The important config values are:

- `percentage`: Alert threshold.
- `topic_id`: OCI Notifications topic OCID.
- `regions`: Optional comma-separated region allowlist.
- `services`: Comma-separated service allowlist. Empty uses the default allowlist; `all` scans every service.
- `limit_names`: Optional per-service limit allowlist in `service:limit1|limit2;service2:limit3` format.
- `max_workers`: Maximum concurrent resource availability calls. Default is `8`.

## Function Invocation Logs

Terraform enables OCI Logging for the Functions application by default. It creates:

- A log group named `<application-name>-logs`.
- A service log named `<application-name>_invoke`.
- 30-day retention by default.

Override the defaults in your Terraform variables file if needed:

```hcl
enable_function_invocation_logs              = true
function_invocation_log_retention_duration   = 30
```

## Notes

- Email subscriptions must be confirmed before alerts are delivered.
- Resource Scheduler requires a dynamic group containing the schedule resource and a policy allowing that dynamic group to manage Functions. The deployment script creates or updates those resources after the schedule exists.
- The function resource principal policy is created by Terraform and allows the function to inspect limits/resource availability and publish to Notifications.
