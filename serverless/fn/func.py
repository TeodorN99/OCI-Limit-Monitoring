import io
import json
import logging
import oci
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import backoff
import os

from fdk import response

DEFAULT_SERVICES = ["compute", "block-storage", "vcn", "load-balancer", "database"]
DEFAULT_MAX_WORKERS = 8

limits_client = None
quotas_client = None
search_client = None
logger = None
identity_client = None
notifications_client = None
resource_principal_signer = None
thread_local = threading.local()


def create_log():
    """ Creates logging file

    Parameters: None

    Returns: A logger instance
    """
    global logger
    if not os.path.exists("/tmp/limits"):
        os.makedirs("/tmp/limits")
    now = datetime.now(timezone.utc)
    LOG_FILENAME = '/tmp/limits/limits'
    filename = LOG_FILENAME + now.strftime('_%Y%m%dT%H%M.log')
    try:
        logging.basicConfig(filename=filename, level=logging.INFO,
                            filemode='w', datefmt='%m/%d/%Y %I:%M:%S%p',
                            format='%(asctime)s %(message)s')
        logger = logging.getLogger("limits")
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s %(message)s', datefmt='%m/%d/%Y %I:%M:%S%p')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except Exception as e:
        logger.info("There is a problem on creating logger: {}".format(e))
    return logger


def initialize(region=None):
    """Creates OCI python sdk clients

    Parameters:
    region - the region in which you want to spawn the config

    Returns:
    limits_client - Client used for making limit requests

    quotas_client - Client used for making quotas requests

    identity_client - Client used for making IAM requests

    search_client - Client used for making search requests

    notifications_client - Client used for making notifications requests

    """

    signer = oci.auth.signers.get_resource_principals_signer()

    global limits_client
    global quotas_client
    global search_client
    global identity_client
    global notifications_client
    global resource_principal_signer
    resource_principal_signer = signer
    identity_client = oci.identity.IdentityClient({}, signer=signer)
    response = identity_client.list_region_subscriptions(signer.tenancy_id)
    for reg in response.data:
        if reg.is_home_region:
            quotas_client = oci.limits.QuotasClient(
                config={"region": reg.region_name}, signer=signer)
            notifications_client = oci.ons.NotificationDataPlaneClient(
                config={"region": reg.region_name}, signer=signer)
            break

    if region != None:
        limits_client = oci.limits.LimitsClient(
            config={"region": region}, signer=signer)
        search_client = oci.resource_search.ResourceSearchClient(
            config={"region": region}, signer=signer)
        identity_client = oci.identity.IdentityClient(
            config={"region": region}, signer=signer)
    else:
        limits_client = oci.limits.LimitsClient({}, signer=signer)
        search_client = oci.resource_search.ResourceSearchClient(
            {}, signer=signer)
        identity_client = oci.identity.IdentityClient({}, signer=signer)
    return signer, limits_client, quotas_client, search_client, identity_client, notifications_client


def get_thread_limits_client(region):
    """Returns a per-thread Limits client for parallel availability checks."""
    clients = getattr(thread_local, "limits_clients", None)
    if clients is None:
        clients = {}
        thread_local.limits_clients = clients
    if region not in clients:
        clients[region] = oci.limits.LimitsClient(
            config={"region": region},
            signer=resource_principal_signer,
        )
    return clients[region]


def is_throttling_error(err):
    """ Returns a bool depending if the status of the error is 429 or not

    Parameters:
    err - Error received from a request

    Returns:
    True or False
    """

    if err.status == 429:
        return False
    return True


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def get_compartment(comp_name):
    """ Gets compartment

    Parameters:
    comp_name - The name of the compartment

    Returns:
    The compartment and its details
    """

    structured_search = oci.resource_search.models.StructuredSearchDetails(query="query compartment resources where displayName='{}'".format(comp_name),
                                                                           type='Structured',
                                                                           matching_context_type=oci.resource_search.models.SearchDetails.MATCHING_CONTEXT_TYPE_NONE)
    comps = search_client.search_resources(structured_search).data
    return comps


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def get_topic(topic_name):
    """ Gets topic

    Parameters:
    topic_name - The name of the compartment

    Returns:
    The topic and its details
    """

    structured_search = oci.resource_search.models.StructuredSearchDetails(query="query onstopic resources where displayName='{}'".format(topic_name),
                                                                           type='Structured',
                                                                           matching_context_type=oci.resource_search.models.SearchDetails.MATCHING_CONTEXT_TYPE_NONE)
    topics = search_client.search_resources(structured_search).data
    return topics


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def list_quotas(compartment_id):
    """ Lists quotas

    Parameters:
    compartment_id: The id of the compartment that should be used for listing quotas

    Returns:
    Quotas for the tenancy
    """
    logger.info(
        "[INFO] Getting quotas for compartment: {}".format(compartment_id))
    return quotas_client.list_quotas(compartment_id=compartment_id).data


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def list_services(tenancy_id):
    """ Lists Services

    Parameters:
    tenancy_id: The id of the tenancy

    Returns:
    Services for a specific compartment
    """
    logger.info("[INFO] Getting services for tenancy: {}".format(tenancy_id))
    return limits_client.list_services(compartment_id=tenancy_id).data


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def list_limit_values(tenancy_id, service_name):
    """ Lists limit values

    Parameters:
    tenancy_id: The id of the tenancy that should be used for listing quotas
    service_name: The name of the service

    Returns:
    Limits for a specific service
    """
    logger.info("[INFO] Getting limits for tenancy: {}".format(tenancy_id))
    return oci.pagination.list_call_get_all_results(limits_client.list_limit_values, compartment_id=tenancy_id, service_name=service_name).data


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def get_resource_availability(tenancy_id, region, service_name, limit_name, ad=None):
    """ Lists quotas

    Parameters:
    tenancy_id: The id of the tenancy that should be used for listing quotas

    Returns:
    Limits for a specific service
    """
    # logger.info("[INFO] Getting percentage for tenancy: {}".format(tenancy_id))
    client = get_thread_limits_client(region)
    if ad != None:
        return client.get_resource_availability(compartment_id=tenancy_id, service_name=service_name, limit_name=limit_name, availability_domain=ad).data
    else:
        return client.get_resource_availability(compartment_id=tenancy_id, service_name=service_name, limit_name=limit_name).data


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def list_limit_definition(tenancy_id, service_name=None):
    """ Lists Limit definitions

    Parameters:
    tenancy_id: The id of the tenancy

    Returns:
    Limit definitions for a specific compartment
    """
    if service_name:
        logger.info("[INFO] Getting limit definitions for service: {}".format(service_name))
        return oci.pagination.list_call_get_all_results(
            limits_client.list_limit_definitions,
            compartment_id=tenancy_id,
            service_name=service_name,
        ).data
    logger.info("[INFO] Getting all limit definitions for tenancy: {}".format(tenancy_id))
    return oci.pagination.list_call_get_all_results(
        limits_client.list_limit_definitions,
        compartment_id=tenancy_id,
    ).data


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def list_availability_domains(tenancy_id):
    logger.info("[INFO] Getting availability domains once for region.")
    return identity_client.list_availability_domains(tenancy_id).data


@backoff.on_exception(backoff.expo, exception=oci.exceptions.ServiceError, max_time=300, giveup=is_throttling_error)
def publish_message(topic_id, body, title):
    """ Publishes message to a topic

    Parameters:
    topic_id: The id of the topic
    body: The body of the message that should be published
    title: The title of the message that should be published

    Returns:
    None
    """
    logger.info("[INFO] Publishing alert to topic.")
    notifications_client.publish_message(topic_id, oci.ons.models.MessageDetails(
        body=body,
        title=title
    ))


def is_throttling_exception(err):
    status = getattr(err, "status", None)
    status_code = getattr(err, "status_code", None)
    return status == 429 or status_code == 429 or str(status_code) == "429"


def csv_values(value):
    if not value:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_values = value
    else:
        raw_values = str(value).split(",")
    return [str(item).strip() for item in raw_values if str(item).strip()]


def dedupe(values):
    seen = set()
    result = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def normalize_services(value):
    services = csv_values(value)
    if not services:
        services = DEFAULT_SERVICES
    if len(services) == 1 and services[0].lower() in ["all", "*"]:
        return []
    return dedupe(services)


def parse_limit_names(value):
    if not value:
        return {}

    filters = {}
    entries = [entry.strip() for entry in str(value).split(";") if entry.strip()]
    for entry in entries:
        if ":" in entry:
            service, names = entry.split(":", 1)
        elif "=" in entry:
            service, names = entry.split("=", 1)
        else:
            logger.info("[WARN] Ignoring limit_names entry without service separator: {}".format(entry))
            continue

        service = service.strip().lower()
        limit_names = [
            name.strip()
            for name in names.replace(",", "|").split("|")
            if name.strip()
        ]
        if not service or not limit_names:
            continue

        filters.setdefault(service, set()).update(
            name.lower() for name in limit_names
        )

    return filters


def parse_max_workers(value):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_MAX_WORKERS


def supports_resource_availability(limit):
    supported = getattr(limit, "is_resource_availability_supported", None)
    return supported is not False


def filter_limit_names(limits, limit_names):
    if not limit_names:
        return limits

    filtered_limits = []
    for limit in limits:
        service_names = limit_names.get(limit.service_name.lower())
        if service_names is None or limit.name.lower() in service_names:
            filtered_limits.append(limit)

    logger.info(
        "[INFO] Using {} of {} supported limit definitions after limit_names filtering.".format(
            len(filtered_limits), len(limits)
        )
    )
    return filtered_limits


def list_target_limit_definitions(tenancy, services, limit_names):
    limits = []
    try:
        if services:
            for service in services:
                try:
                    limits.extend(list_limit_definition(tenancy, service))
                except Exception as e:
                    if is_throttling_exception(e):
                        raise
                    if getattr(e, "status", None) == 400:
                        logger.info("[WARN] Skipping service {}: {}".format(service, e))
                        continue
                    raise
        else:
            limits = list_limit_definition(tenancy)
    except Exception as e:
        logger.info(e)
        if is_throttling_exception(e):
            raise
    supported_limits = [
        limit for limit in limits
        if supports_resource_availability(limit)
    ]
    logger.info(
        "[INFO] Using {} of {} limit definitions after resource availability filtering.".format(
            len(supported_limits), len(limits)
        )
    )
    return filter_limit_names(supported_limits, limit_names)


def availability_numbers(resource_availability):
    used = getattr(resource_availability, "fractional_usage", None)
    available = getattr(resource_availability, "fractional_availability", None)
    if used is None:
        used = getattr(resource_availability, "used", None)
    if available is None:
        available = getattr(resource_availability, "available", None)
    if used is None or available is None:
        return None, None
    return float(used), float(available)


def build_availability_checks(limits, ads):
    checks = []
    for limit in limits:
        if limit.scope_type == "AD":
            for ad in ads:
                checks.append((limit, ad.name))
        else:
            checks.append((limit, None))
    return checks


def fetch_availability(tenancy, region, limit, ad):
    resource_availability = get_resource_availability(
        tenancy,
        region,
        limit.service_name,
        limit.name,
        ad,
    )
    return limit, ad, resource_availability


def record_availability(limit_values, body_email, region, percentage, limit, ad, resource_availability):
    used, available = availability_numbers(resource_availability)
    if used is None or available is None or used + available <= 0:
        return

    total_available = available * 100 / (used + available)
    key_parts = [limit.service_name, limit.name, region]
    if ad:
        key_parts.append(ad)
    key = "_".join(key_parts)
    limit_values[key] = "Available Resources: {:.2f}%".format(total_available)

    if ad:
        logger.info("Service {}       AD {}       Limit_Name {}       Available {}       Used {}       Total {:.2f}%".format(
            limit.service_name, ad, limit.name, available, used, total_available))
        body = "Limit reached for {}. Info: Service {}, Scope {}, AD {}, Limit_Name {}, Available {}, Used {}, Total {:.2f}%".format(
            limit.name, limit.service_name, limit.scope_type, ad, limit.name, available, used, total_available)
    else:
        logger.info("Service {}       Scope {}       Limit_Name {}       Available {}       Used {}       Total {:.2f}%".format(
            limit.service_name, limit.scope_type, limit.name, available, used, total_available))
        body = "Limit reached for {}. Info: Service {}, Scope {}, Limit_Name {}, Available {}, Used {}, Total {:.2f}%".format(
            limit.name, limit.service_name, limit.scope_type, limit.name, available, used, total_available)

    if total_available < percentage:
        body_email.append(body)


def check_limits(tenancy, topic_id, region, percentage, services, limit_names, max_workers):
    limit_values = {}
    body_email = []
    limits = list_target_limit_definitions(tenancy, services, limit_names)
    ads = []

    if any(limit.scope_type == "AD" for limit in limits):
        try:
            ads = list_availability_domains(tenancy)
        except Exception as e:
            logger.info(e)
            if is_throttling_exception(e):
                raise

    checks = build_availability_checks(limits, ads)
    logger.info("[INFO] Checking {} resource availability values with up to {} workers.".format(
        len(checks), max_workers))

    if len(checks) == 0:
        return limit_values

    worker_count = min(max_workers, len(checks))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(fetch_availability, tenancy, region, limit, ad)
            for limit, ad in checks
        ]
        for future in as_completed(futures):
            try:
                limit, ad, resource_availability = future.result()
                record_availability(
                    limit_values,
                    body_email,
                    region,
                    percentage,
                    limit,
                    ad,
                    resource_availability,
                )
            except Exception as e:
                logger.info(e)
                if is_throttling_exception(e):
                    raise

    title = "Region {} limit threshold exceeded at {}{}".format(
        region, percentage, '%')
    message_body = "\n\n".join(str(body) for body in body_email)
    if len(message_body) > 0:
        publish_message(topic_id, message_body, title)
    return limit_values


def main(regions, topic_id, percentage, services, limit_names, max_workers):
    signer, limits_client, quotas_client, search_client, identity_client, notifications_client = initialize()
    tenancy = signer.tenancy_id
    region_data = identity_client.list_region_subscriptions(tenancy)
    limits = []
    region_list = csv_values(regions)
    for reg in region_data.data:
        signer, limits_client, quotas_client, search_client, identity_client, notifications_client = initialize(
            region=reg.region_name)
        if len(region_list) == 0:
            limit_values = check_limits(
                tenancy, topic_id, reg.region_name, percentage, services, limit_names, max_workers)
            limits.append(limit_values)
        else:
            if reg.region_name in region_list:
                limit_values = check_limits(
                    tenancy, topic_id, reg.region_name, percentage, services, limit_names, max_workers)
                limits.append(limit_values)
    return limits


def handler(ctx, data: io.BytesIO = None):
    config = ctx.Config()
    percentage = config["percentage"]
    regions = config.get("regions", "")
    if "topic_id" not in config:
        exit(1)
    else:
        topic_id = config["topic_id"]

    create_log()

    services = normalize_services(config.get("services", ""))
    limit_names = parse_limit_names(config.get("limit_names", ""))
    max_workers = parse_max_workers(config.get("max_workers", DEFAULT_MAX_WORKERS))

    logger.info("[INFO] Target services: {}".format(",".join(services) if services else "all"))
    if limit_names:
        logger.info("[INFO] Limit name filters: {}".format(
            ";".join(
                "{}:{}".format(service, ",".join(sorted(names)))
                for service, names in sorted(limit_names.items())
            )
        ))
    logger.info("[INFO] Max workers: {}".format(max_workers))

    limits = main(regions, topic_id, int(percentage), services, limit_names, max_workers)

    return response.Response(
        ctx, response_data=json.dumps(limits),
        headers={"Content-Type": "application/json"}
    )
