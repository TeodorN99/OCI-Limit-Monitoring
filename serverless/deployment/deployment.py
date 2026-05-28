import argparse
import getpass
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import oci
from jinja2 import Template
from oci.config import from_file
from oci.pagination import list_call_get_all_results


identity_client = None
fn_mgmt_client = None
os_client = None
schedule_client = None
search_client = None

DEFAULT_SERVICES = "compute,block-storage,vcn,load-balancer,database"
DEFAULT_MAX_WORKERS = 8


def regional_endpoint(region_name, endpoint_template):
    return oci.regions.endpoint_for(
        "custom",
        region_name,
        service_endpoint_template=endpoint_template,
    ).rstrip("/")


def endpoint_host(endpoint):
    return re.sub(r"^https?://", "", endpoint).rstrip("/")


def functions_endpoint(region_name):
    return regional_endpoint(
        region_name,
        "https://functions.{region}.oci.{secondLevelDomain}",
    )


def ocir_registry_host(region_name):
    return endpoint_host(
        regional_endpoint(
            region_name,
            "https://ocir.{region}.oci.{secondLevelDomain}",
        )
    )


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9_]", "_", value)[:100]


def run(command, cwd=None, stdin=None):
    print("[INFO] Running: {}".format(" ".join(command)))
    result = subprocess.run(
        command,
        cwd=cwd,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr)
        raise RuntimeError("Command failed: {}".format(" ".join(command)))
    return result


def require_command(command_name, install_hint):
    if shutil.which(command_name):
        return
    raise RuntimeError(
        "Required command '{}' was not found on PATH.\n{}".format(
            command_name, install_hint
        )
    )


def get_container_cli(container_cli):
    if container_cli != "auto":
        require_command(
            container_cli,
            "Install {} or choose a different -container_cli value.".format(container_cli),
        )
        return container_cli

    for candidate in ["docker", "podman"]:
        if shutil.which(candidate):
            return candidate

    raise RuntimeError(
        "Neither docker nor podman was found on PATH. Install one of them or use OCI Cloud Shell."
    )


def preflight(args):
    require_command(
        "fn",
        "Install the Fn CLI and open a new terminal before rerunning deployment.py. "
        "On Windows, one common option is: scoop install fnproject",
    )
    return get_container_cli(args.container_cli)


def get_ocir_password(args):
    if args.password:
        print("[WARN] Passing -password exposes the token in the deployment.py process arguments. Prefer omitting it and using the hidden prompt.")
        return args.password

    if args.password_env:
        password = os.environ.get(args.password_env, "")
        if password:
            return password

    return getpass.getpass("OCIR auth token: ")


def get_oci_auth(auth, profile_name, config_file, region, tenancy_id):
    if auth == "api_key":
        return from_file(file_location=config_file, profile_name=profile_name), None

    if not region:
        raise RuntimeError("{} auth requires -region or OCI_REGION/OCI_CLI_REGION.".format(auth))
    if not tenancy_id:
        raise RuntimeError("{} auth requires -tenancy_id.".format(auth))

    if auth == "cloud_shell":
        delegation_token_path = "/etc/oci/delegation_token"
        if not os.path.exists(delegation_token_path):
            raise RuntimeError("Cloud Shell auth requires {}.".format(delegation_token_path))
        with open(delegation_token_path, "r", encoding="utf-8") as token_file:
            delegation_token = token_file.read().strip()
        signer = oci.auth.signers.InstancePrincipalsDelegationTokenSigner(
            delegation_token=delegation_token
        )
        return {"region": region, "tenancy": tenancy_id}, signer

    signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
    return {"region": region, "tenancy": tenancy_id}, signer


def initialize(auth, profile_name, config_file, region, tenancy_id):
    """Creates OCI SDK clients in the tenancy home region."""
    config, signer = get_oci_auth(auth, profile_name, config_file, region, tenancy_id)

    global identity_client
    global fn_mgmt_client
    global os_client
    global schedule_client
    global search_client

    client_kwargs = {"signer": signer} if signer else {}
    identity_client = oci.identity.IdentityClient(config, **client_kwargs)
    regions = identity_client.list_region_subscriptions(config["tenancy"]).data
    home_region = [region for region in regions if region.is_home_region][0]

    config["region"] = home_region.region_name
    identity_client = oci.identity.IdentityClient(config, **client_kwargs)
    fn_mgmt_client = oci.functions.FunctionsManagementClient(config, **client_kwargs)
    os_client = oci.object_storage.ObjectStorageClient(config, **client_kwargs)
    schedule_client = oci.resource_scheduler.ScheduleClient(config, **client_kwargs)
    search_client = oci.resource_search.ResourceSearchClient(config, **client_kwargs)

    return config, home_region


def get_application(compartment_id, app_name):
    applications = list_call_get_all_results(
        fn_mgmt_client.list_applications,
        compartment_id=compartment_id,
        display_name=app_name,
    ).data
    if not applications:
        raise RuntimeError("Unable to find Functions application '{}'".format(app_name))
    return applications[0]


def get_function(compartment_id, app_name, function_name, attempts=12, delay_seconds=5):
    application = get_application(compartment_id, app_name)
    for attempt in range(1, attempts + 1):
        functions = list_call_get_all_results(
            fn_mgmt_client.list_functions,
            application_id=application.id,
            display_name=function_name,
        ).data
        if functions:
            return functions[0]

        print(
            "[INFO] Waiting for function '{}' to become visible ({}/{})".format(
                function_name, attempt, attempts
            )
        )
        time.sleep(delay_seconds)

    raise RuntimeError(
        "Unable to find deployed function '{}' in app '{}'".format(
            function_name, app_name
        )
    )


def write_func_yaml(function_name, topic_id, percentage, regions, services, limit_names, max_workers, timeout, memory):
    fn_config = """schema_version: 20180708
name: {{ function_name }}
version: 0.1.0
runtime: python
entrypoint: /python/bin/fdk /function/func.py handler
memory: {{ memory }}
timeout: {{ timeout }}
config:
  percentage: "{{ percentage }}"
  topic_id: {{ topic_id }}
  max_workers: "{{ max_workers }}"
{% if regions %}  regions: {{ regions }}
{% endif %}{% if services %}  services: {{ services }}
{% endif %}{% if limit_names %}  limit_names: "{{ limit_names }}"
{% endif %}"""
    message = Template(fn_config).render(
        function_name=function_name,
        topic_id=topic_id,
        percentage=percentage,
        regions=regions,
        services=services,
        limit_names=limit_names,
        max_workers=max_workers,
        timeout=timeout,
        memory=memory,
    )
    fn_dir = Path(__file__).resolve().parents[1] / "fn"
    (fn_dir / "func.yaml").write_text(message, encoding="utf-8")
    return fn_dir


def configure_fn_context(args, config, home_region, container_cli):
    namespace = os_client.get_namespace().data
    context_name = args.fn_context
    registry_host = ocir_registry_host(home_region.region_name)
    registry_path = "{}/{}/limits".format(registry_host, namespace)
    api_url = functions_endpoint(home_region.region_name)

    contexts = run(["fn", "list", "contexts"]).stdout
    if context_name not in contexts:
        run(["fn", "create", "context", context_name, "--provider", args.fn_provider])

    context_is_active = any(
        line.lstrip().startswith("*") and context_name in line
        for line in contexts.splitlines()
    )
    if not context_is_active:
        run(["fn", "use", "context", context_name])
    else:
        print("[INFO] Fn context {} is already active.".format(context_name))

    run(["fn", "update", "context", "oracle.compartment-id", args.compartment_id])
    run(["fn", "update", "context", "oracle.image-compartment-id", args.image_compartment_id or args.compartment_id])
    run(["fn", "update", "context", "api-url", api_url])
    run(["fn", "update", "context", "registry", registry_path])

    if args.skip_docker_login:
        print("[INFO] Skipping docker login. Make sure the current environment can push to OCIR.")
        return

    if not args.user:
        raise RuntimeError("Provide -user, or use -skip_docker_login if OCIR is already authenticated.")

    password = get_ocir_password(args)
    if not password:
        raise RuntimeError("Provide an OCIR auth token with the hidden prompt, -password_env, or -password.")

    run(
        [container_cli, "login", "-u", args.user, "--password-stdin", registry_host],
        stdin=password,
    )


def create_or_update_schedule(compartment_id, function_id, schedule_name, recurrence_type, recurrence_details):
    resources = [oci.resource_scheduler.models.Resource(id=function_id)]
    existing = list_call_get_all_results(
        schedule_client.list_schedules,
        compartment_id=compartment_id,
        display_name=schedule_name,
    ).data
    existing = collection_items(existing)

    if existing:
        schedule = existing[0]
        print("[INFO] Updating Resource Scheduler schedule {}".format(schedule.id))
        schedule_client.update_schedule(
            schedule.id,
            oci.resource_scheduler.models.UpdateScheduleDetails(
                action=oci.resource_scheduler.models.UpdateScheduleDetails.ACTION_START_RESOURCE,
                recurrence_type=recurrence_type,
                recurrence_details=recurrence_details,
                resources=resources,
                description="Invokes the OCI limit monitoring function.",
            ),
        )
        return schedule_client.get_schedule(schedule.id).data

    print("[INFO] Creating Resource Scheduler schedule {}".format(schedule_name))
    return schedule_client.create_schedule(
        oci.resource_scheduler.models.CreateScheduleDetails(
            compartment_id=compartment_id,
            display_name=schedule_name,
            description="Invokes the OCI limit monitoring function.",
            action=oci.resource_scheduler.models.CreateScheduleDetails.ACTION_START_RESOURCE,
            recurrence_type=recurrence_type,
            recurrence_details=recurrence_details,
            resources=resources,
        )
    ).data


def get_resource_id(resource):
    return getattr(resource, "id", None) or getattr(resource, "identifier", None)


def collection_items(data):
    return data.items if hasattr(data, "items") else data


def ensure_scheduler_iam(tenancy_id, schedule_id, schedule_name):
    dynamic_group_name = safe_name("{}_scheduler_dg".format(schedule_name))
    policy_name = safe_name("{}_scheduler_policy".format(schedule_name))
    matching_rule = "ALL {{resource.type = 'resourceschedule', resource.id = '{}'}}".format(schedule_id)
    statements = [
        "Allow dynamic-group {} to manage functions-family in tenancy".format(dynamic_group_name)
    ]

    dynamic_groups = list_call_get_all_results(
        identity_client.list_dynamic_groups,
        compartment_id=tenancy_id,
        name=dynamic_group_name,
    ).data

    if dynamic_groups:
        print("[INFO] Updating scheduler dynamic group {}".format(dynamic_group_name))
        identity_client.update_dynamic_group(
            dynamic_groups[0].id,
            oci.identity.models.UpdateDynamicGroupDetails(
                description="Resource Scheduler principal for limit monitoring.",
                matching_rule=matching_rule,
            ),
        )
    else:
        print("[INFO] Creating scheduler dynamic group {}".format(dynamic_group_name))
        identity_client.create_dynamic_group(
            oci.identity.models.CreateDynamicGroupDetails(
                compartment_id=tenancy_id,
                name=dynamic_group_name,
                description="Resource Scheduler principal for limit monitoring.",
                matching_rule=matching_rule,
            )
        )

    policies = list_call_get_all_results(
        identity_client.list_policies,
        compartment_id=tenancy_id,
        name=policy_name,
    ).data

    if policies:
        print("[INFO] Updating scheduler policy {}".format(policy_name))
        identity_client.update_policy(
            policies[0].id,
            oci.identity.models.UpdatePolicyDetails(
                description="Allows Resource Scheduler to invoke limit monitoring.",
                statements=statements,
            ),
        )
    else:
        print("[INFO] Creating scheduler policy {}".format(policy_name))
        identity_client.create_policy(
            oci.identity.models.CreatePolicyDetails(
                compartment_id=tenancy_id,
                name=policy_name,
                description="Allows Resource Scheduler to invoke limit monitoring.",
                statements=statements,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description="Deploys one OCI limit monitoring function and schedules it with OCI Resource Scheduler.",
    )
    parser.add_argument("-auth", dest="auth", default="api_key", choices=["api_key", "cloud_shell", "instance_principal"], help="OCI SDK auth mode.")
    parser.add_argument("-profile", dest="profile", default="DEFAULT", help="OCI config profile name for api_key auth.")
    parser.add_argument("-config_file", dest="config_file", default=os.path.expanduser("~/.oci/config"), help="OCI config file path.")
    parser.add_argument("-region", dest="region", default=os.environ.get("OCI_REGION") or os.environ.get("OCI_CLI_REGION") or "", help="Region used to bootstrap cloud_shell or instance_principal auth.")
    parser.add_argument("-tenancy_id", dest="tenancy_id", default="", help="Tenancy OCID. Required for cloud_shell and instance_principal auth.")
    parser.add_argument("-user", dest="user", type=str, default="", help="OCIR user. Usually tenancy_namespace/user_email.")
    parser.add_argument("-password", dest="password", type=str, default="", help="OCIR auth token. Prefer omitting this and using the hidden prompt.")
    parser.add_argument("-password_env", dest="password_env", default="OCIR_TOKEN", help="Environment variable containing the OCIR auth token. Used if -password is omitted.")
    parser.add_argument("-compartment_id", dest="compartment_id", type=str, required=True, help="Compartment OCID for the Functions app and schedule.")
    parser.add_argument("-image_compartment_id", dest="image_compartment_id", default="", help="Compartment OCID for OCIR function images. Defaults to -compartment_id.")
    parser.add_argument("-app_name", dest="app_name", type=str, required=True, help="Functions application name.")
    parser.add_argument("-topic_id", dest="topic_id", type=str, required=True, help="Notification topic OCID.")
    parser.add_argument("-percentage", dest="percentage", type=int, required=True, help="Alert threshold percentage.")
    parser.add_argument("-function_name", dest="function_name", default="limit-monitoring", help="Function name to deploy.")
    parser.add_argument("-regions", dest="regions", default="", help="Optional comma-separated region list. Empty means all subscribed regions.")
    parser.add_argument("-services", dest="services", default=DEFAULT_SERVICES, help="Comma-separated OCI service names to check. Use 'all' to scan every service.")
    parser.add_argument("-limit_names", dest="limit_names", default="", help="Optional per-service limit allowlist. Format: service:limit1|limit2;service2:limit3")
    parser.add_argument("-max_workers", dest="max_workers", type=int, default=DEFAULT_MAX_WORKERS, help="Maximum concurrent GetResourceAvailability calls.")
    parser.add_argument("-schedule_name", dest="schedule_name", default="limit-monitoring-weekly", help="Resource Scheduler schedule name.")
    parser.add_argument("-recurrence_type", dest="recurrence_type", default="CRON", choices=["CRON", "ICAL"], help="Resource Scheduler recurrence type.")
    parser.add_argument("-recurrence_details", dest="recurrence_details", default="0 7 * * 1", help="UTC recurrence. Default is every Monday at 07:00 UTC.")
    parser.add_argument("-timeout", dest="timeout", type=int, default=300, help="Function timeout in seconds.")
    parser.add_argument("-memory", dest="memory", type=int, default=1024, help="Function memory in MB.")
    parser.add_argument("-fn_context", dest="fn_context", default="limit_context", help="Fn CLI context name.")
    parser.add_argument("-fn_provider", dest="fn_provider", default="oracle-cs", choices=["oracle-cs", "oracle", "oracle-ip"], help="Fn context provider. Use oracle-cs in Cloud Shell and oracle on a local machine.")
    parser.add_argument("-skip_docker_login", dest="skip_docker_login", action="store_true", help="Skip docker login when the environment is already authenticated to OCIR.")
    parser.add_argument("-container_cli", dest="container_cli", default="auto", choices=["auto", "docker", "podman"], help="Container CLI used for OCIR login. Auto prefers docker, then podman.")

    args = parser.parse_args()

    container_cli = preflight(args)
    config, home_region = initialize(args.auth, args.profile, args.config_file, args.region, args.tenancy_id)
    configure_fn_context(args, config, home_region, container_cli)
    fn_dir = write_func_yaml(
        args.function_name,
        args.topic_id,
        args.percentage,
        args.regions,
        args.services,
        args.limit_names,
        args.max_workers,
        args.timeout,
        args.memory,
    )

    print("[INFO] Deploying single all-regions function {}".format(args.function_name))
    run(["fn", "deploy", "--app", args.app_name], cwd=fn_dir)

    function = get_function(args.compartment_id, args.app_name, args.function_name)
    schedule = create_or_update_schedule(
        args.compartment_id,
        get_resource_id(function),
        args.schedule_name,
        args.recurrence_type,
        args.recurrence_details,
    )
    ensure_scheduler_iam(config["tenancy"], schedule.id, args.schedule_name)

    print("[INFO] Function OCID: {}".format(get_resource_id(function)))
    print("[INFO] Resource schedule OCID: {}".format(schedule.id))
