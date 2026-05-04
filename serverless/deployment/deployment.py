import argparse
import os
import re
import shutil
import subprocess
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


def initialize(profile_name, config_file):
    """Creates OCI SDK clients in the tenancy home region."""
    config = from_file(file_location=config_file, profile_name=profile_name)

    global identity_client
    global fn_mgmt_client
    global os_client
    global schedule_client
    global search_client

    identity_client = oci.identity.IdentityClient(config)
    regions = identity_client.list_region_subscriptions(config["tenancy"]).data
    home_region = [region for region in regions if region.is_home_region][0]

    config["region"] = home_region.region_name
    identity_client = oci.identity.IdentityClient(config)
    fn_mgmt_client = oci.functions.FunctionsManagementClient(config)
    os_client = oci.object_storage.ObjectStorageClient(config)
    schedule_client = oci.resource_scheduler.ScheduleClient(config)
    search_client = oci.resource_search.ResourceSearchClient(config)

    return config, home_region


def get_function(function_name):
    structured_search = oci.resource_search.models.StructuredSearchDetails(
        query="query functionsfunction resources where displayName='{}'".format(function_name),
        type="Structured",
        matching_context_type=oci.resource_search.models.SearchDetails.MATCHING_CONTEXT_TYPE_NONE,
    )
    functions = search_client.search_resources(structured_search).data.items
    if not functions:
        raise RuntimeError("Unable to find deployed function '{}'".format(function_name))
    return functions[0]


def write_func_yaml(function_name, topic_id, percentage, regions, services, timeout, memory):
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
{% if regions %}  regions: {{ regions }}
{% endif %}{% if services %}  services: {{ services }}
{% endif %}"""
    message = Template(fn_config).render(
        function_name=function_name,
        topic_id=topic_id,
        percentage=percentage,
        regions=regions,
        services=services,
        timeout=timeout,
        memory=memory,
    )
    fn_dir = Path(__file__).resolve().parents[1] / "fn"
    (fn_dir / "func.yaml").write_text(message, encoding="utf-8")
    return fn_dir


def configure_fn_context(args, config, home_region, container_cli):
    namespace = os_client.get_namespace().data
    context_name = args.fn_context

    contexts = run(["fn", "list", "contexts"]).stdout
    if context_name not in contexts:
        run(["fn", "create", "context", context_name, "--provider", args.fn_provider])

    run(["fn", "use", "context", context_name])
    run(["fn", "update", "context", "oracle.compartment-id", args.compartment_id])
    run(["fn", "update", "context", "oracle.image-compartment-id", args.image_compartment_id or args.compartment_id])
    run(["fn", "update", "context", "api-url", "https://functions.{}.oci.oraclecloud.com".format(home_region.region_name)])
    run(["fn", "update", "context", "registry", "{}.ocir.io/{}/limits".format(str(home_region.region_key).lower(), namespace)])

    if args.skip_docker_login:
        print("[INFO] Skipping docker login. Make sure the current environment can push to OCIR.")
        return

    if not args.user or not args.password:
        raise RuntimeError("Provide -user and -password, or use -skip_docker_login if OCIR is already authenticated.")

    run(
        [container_cli, "login", "-u", args.user, "--password-stdin", "{}.ocir.io".format(str(home_region.region_key).lower())],
        stdin=args.password,
    )


def create_or_update_schedule(compartment_id, function_id, schedule_name, recurrence_type, recurrence_details):
    resources = [oci.resource_scheduler.models.Resource(id=function_id)]
    existing = list_call_get_all_results(
        schedule_client.list_schedules,
        compartment_id=compartment_id,
        display_name=schedule_name,
    ).data.items

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
    parser.add_argument("-profile", dest="profile", default="DEFAULT", help="OCI config profile name.")
    parser.add_argument("-config_file", dest="config_file", default=os.path.expanduser("~/.oci/config"), help="OCI config file path.")
    parser.add_argument("-user", dest="user", type=str, default="", help="OCIR user. Usually tenancy_namespace/user_email.")
    parser.add_argument("-password", dest="password", type=str, default="", help="OCIR auth token.")
    parser.add_argument("-compartment_id", dest="compartment_id", type=str, required=True, help="Compartment OCID for the Functions app and schedule.")
    parser.add_argument("-image_compartment_id", dest="image_compartment_id", default="", help="Compartment OCID for OCIR function images. Defaults to -compartment_id.")
    parser.add_argument("-app_name", dest="app_name", type=str, required=True, help="Functions application name.")
    parser.add_argument("-topic_id", dest="topic_id", type=str, required=True, help="Notification topic OCID.")
    parser.add_argument("-percentage", dest="percentage", type=int, required=True, help="Alert threshold percentage.")
    parser.add_argument("-function_name", dest="function_name", default="limit-monitoring", help="Function name to deploy.")
    parser.add_argument("-regions", dest="regions", default="", help="Optional comma-separated region list. Empty means all subscribed regions.")
    parser.add_argument("-services", dest="services", default="", help="Optional comma-separated OCI service names to check.")
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
    config, home_region = initialize(args.profile, args.config_file)
    configure_fn_context(args, config, home_region, container_cli)
    fn_dir = write_func_yaml(
        args.function_name,
        args.topic_id,
        args.percentage,
        args.regions,
        args.services,
        args.timeout,
        args.memory,
    )

    print("[INFO] Deploying single all-regions function {}".format(args.function_name))
    run(["fn", "deploy", "--app", args.app_name], cwd=fn_dir)

    function = get_function(args.function_name)
    schedule = create_or_update_schedule(
        args.compartment_id,
        function.identifier,
        args.schedule_name,
        args.recurrence_type,
        args.recurrence_details,
    )
    ensure_scheduler_iam(config["tenancy"], schedule.id, args.schedule_name)

    print("[INFO] Function OCID: {}".format(function.identifier))
    print("[INFO] Resource schedule OCID: {}".format(schedule.id))
