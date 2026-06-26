import json
from google.cloud import run_v2, scheduler_v1
from time import sleep
from typing import Dict

local_config_file_name = "cloud_run_config.json"

default_location = "europe-west1"
location = default_location
project_id = "crea-aq-data"
project_path = f"projects/{project_id}/locations/{default_location}"
full_project_job_name = project_path + "/jobs/{job_name}"
service_account_email = "829505003332-compute@developer.gserviceaccount.com"

nfs_volume_name = "crea-nfs"
nfs_path = "/crea_nfs"
nfs_mount_path = f"/mnt{nfs_path}"
nfs_server = "10.21.193.2"

# Ownership marker. Only jobs carrying this annotation are managed by this
# script; every other job (e.g. ones created manually in the console) is left
# untouched. A domain-prefixed key is used because Cloud Run labels disallow
# dots/slashes, whereas Knative-style annotations permit a DNS prefix.
MANAGED_BY_KEY = "energyandcleanair.org/managed-by"
MANAGED_BY_VALUE = "cron-jobs-config"


def main():
    run_client = run_v2.JobsClient()
    scheduler_client = scheduler_v1.CloudSchedulerClient()

    local_config = load_json(local_config_file_name)
    locations = get_config_locations(local_config)
    jobs, schedulers = get_remote_jobs_and_schedulers(run_client, scheduler_client, locations)
    managed_jobs = filter_managed_jobs(jobs)

    skipped = sorted(set(jobs) - set(managed_jobs))
    if skipped:
        print(f"Ignoring {len(skipped)} unmanaged job(s): {', '.join(format_job_key(job) for job in skipped)}")

    remote_config = generate_current_config(managed_jobs, schedulers)

    remote_config.sort(key=sort_config_key)
    local_config.sort(key=sort_config_key)

    diff = check_diff(remote_config, local_config)

    for action in ["inserted", "updated", "removed"]:
        if diff[action]:
            for k, item in diff[action]:
                prev_item = next((x for x in remote_config if config_key(x) == config_key(item)), {})
                create_job(run_client, action, item)
                create_scheduler_job(scheduler_client, action, prev_item, item)

    print(diff)


def get_location(item: dict) -> str:
    return item.get("region") or default_location


def get_config_locations(config: list[dict]) -> list[str]:
    return sorted({default_location, *(get_location(item) for item in config)})


def project_path_for(location: str) -> str:
    return f"projects/{project_id}/locations/{location}"


def full_project_job_name_for(location: str, job_name: str) -> str:
    return f"{project_path_for(location)}/jobs/{job_name}"


def scheduler_endpoint_for(location: str, job_name: str) -> str:
    return (
        f"https://{location}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{project_id}"
        f"/jobs/{job_name}:run"
    )


def nfs_connector_for(location: str) -> str:
    return f"{project_path_for(location)}/connectors/nfs-connector"


def get_resource_location(resource_name: str) -> str:
    parts = resource_name.split("/")
    return parts[parts.index("locations") + 1]


def job_key_from_resource(resource_name: str) -> tuple[str, str]:
    return (get_resource_location(resource_name), resource_name.split("/")[-1])


def config_key(item: dict) -> tuple[str, str]:
    return (get_location(item), item["name"])


def sort_config_key(item: dict) -> tuple[str, str]:
    return config_key(item)


def format_job_key(job_key: tuple[str, str]) -> str:
    location, name = job_key
    return f"{location}/{name}"


def get_remote_jobs_and_schedulers(run_client, scheduler_client, locations=None):
    locations = locations or [default_location]
    jobs = {}
    schedulers = {}

    for location in locations:
        parent = project_path_for(location)
        jobs_response = run_client.list_jobs(request=run_v2.ListJobsRequest(parent=parent, page_size=500))
        schedulers_response = scheduler_client.list_jobs(
            request=scheduler_v1.ListJobsRequest(parent=parent, page_size=500)
        )

        jobs.update({job_key_from_resource(job.name): job for job in jobs_response})
        schedulers.update({job_key_from_resource(scheduler.name): scheduler for scheduler in schedulers_response})

    print(f"Found {len(jobs)} jobs and {len(schedulers)} schedulers.")
    return jobs, schedulers


def is_managed(job) -> bool:
    """A job is managed by this script iff it carries the ownership annotation."""
    return dict(job.annotations).get(MANAGED_BY_KEY) == MANAGED_BY_VALUE


def filter_managed_jobs(jobs: Dict[str, run_v2.Job]) -> Dict[str, run_v2.Job]:
    """Keep only the jobs this script owns; unmanaged jobs are ignored entirely."""
    return {name: job for name, job in jobs.items() if is_managed(job)}


def generate_current_config(jobs: Dict[str, run_v2.Job], schedulers: Dict[str, scheduler_v1.Job]):
    output = []

    # join jobs and schedulers based on name
    for job_key, job in jobs.items():
        scheduler = schedulers.get(job_key, None)
        location = get_resource_location(job.name)

        uses_nfs = job.template.template.vpc_access.connector not in [None, ""]
        # skip first command arg if nfs is used
        index_args = 1 if uses_nfs else 0
        command = " ".join(job.template.template.containers[0].args[index_args:])

        item = {
            "name": job.name.split("/")[-1],
            "schedule": scheduler.schedule if scheduler else "",
            "time_zone": scheduler.time_zone if scheduler else "",
            "image": job.template.template.containers[0].image,
            "command": command,
            "parallelism": job.template.parallelism,
            "taskCount": job.template.task_count,
            "maxRetries": job.template.template.max_retries,
            "timeoutSeconds": int(job.template.template.timeout.total_seconds()),
            "cpu": job.template.template.containers[0].resources.limits["cpu"],
            "memory": job.template.template.containers[0].resources.limits["memory"],
            "nfs": uses_nfs,
        }
        if location != default_location:
            item["region"] = location

        output.append(item)

    return output


def create_job(run_client, action, item):
    location = get_location(item)

    if action == "inserted":
        job = construct_job(item)
        job.name = None
        create_request = run_v2.CreateJobRequest(job=job, parent=project_path_for(location), job_id=item["name"])
        response = run_client.create_job(request=create_request)
        action_msg = "Created job"

    elif action == "updated":
        update_request = run_v2.UpdateJobRequest(job=construct_job(item))
        response = run_client.update_job(request=update_request)
        action_msg = "Updated job"

    elif action == "removed":
        delete_request = run_v2.DeleteJobRequest(name=full_project_job_name_for(location, item["name"]))
        response = run_client.delete_job(request=delete_request)
        action_msg = "Deleted job"

    print(f"{action_msg}: {response.result().name}")
    sleep(2)


def create_scheduler_job(scheduler_client, action: str, prev_item: dict, item: dict):
    location = get_location(item)

    if not prev_item:
        action = "inserted"
    elif prev_item["schedule"] and not item["schedule"]:
        action = "removed"
    elif prev_item["schedule"] != item["schedule"]:
        action = "updated"

    if action == "inserted":
        job = construct_scheduler(item)
        create_request = scheduler_v1.CreateJobRequest(parent=project_path_for(location), job=job)
        scheduler_client.create_job(request=create_request)
        action_msg = "Created scheduler job"

    elif action == "updated":
        update_request = scheduler_v1.UpdateJobRequest(job=construct_scheduler(item))
        scheduler_client.update_job(request=update_request)
        action_msg = "Updated scheduler job"

    elif action == "removed":
        delete_request = scheduler_v1.DeleteJobRequest(name=full_project_job_name_for(location, item["name"]))
        scheduler_client.delete_job(request=delete_request)
        action_msg = "Deleted scheduler job"

    print(f"{action_msg}: {item['name']}")
    sleep(2)


def construct_scheduler(item: dict):
    location = get_location(item)

    job = scheduler_v1.Job()
    job.name = full_project_job_name_for(location, item["name"])
    job.schedule = item["schedule"]
    job.time_zone = item["time_zone"]
    job.http_target = scheduler_v1.HttpTarget()
    job.http_target.uri = scheduler_endpoint_for(location, item["name"])
    job.http_target.http_method = scheduler_v1.HttpMethod.POST
    job.http_target.oauth_token.service_account_email = service_account_email
    return job


def construct_job(item: dict):
    location = get_location(item)

    job = run_v2.Job()
    job.name = full_project_job_name_for(location, item["name"])
    # Stamp the ownership annotation so this job is recognised as managed on later runs.
    job.annotations = {MANAGED_BY_KEY: MANAGED_BY_VALUE}
    job.template = run_v2.ExecutionTemplate()
    job.template.template = run_v2.TaskTemplate()

    job.template.parallelism = item["parallelism"]
    job.template.task_count = item["taskCount"]
    job.template.template.max_retries = item["maxRetries"]
    job.template.template.timeout = {"seconds": item["timeoutSeconds"]}
    job.template.template.service_account = service_account_email
    job.template.template.execution_environment = run_v2.ExecutionEnvironment.EXECUTION_ENVIRONMENT_GEN2
    container = run_v2.Container()
    container.image = item["image"]
    # container.args = item["command"].split(" ")
    # container.command = ["python3"]
    container.resources.limits = {"cpu": item["cpu"], "memory": item["memory"]}

    container.command = []  # use args[0] as the executable
    container.args = ["/app/config/run.sh"] + item["command"].split(" ")

    if item["nfs"]:
        vpc = run_v2.VpcAccess()
        vpc.connector = nfs_connector_for(location)
        job.template.template.vpc_access = vpc

        # Add NFS VPC startup script
        nfs_volume = run_v2.Volume(
            name=nfs_volume_name,
            nfs=run_v2.NFSVolumeSource(
                server=nfs_server,
                path=nfs_path,
                read_only=False
            )
        )

        job.template.template.volumes.append(nfs_volume)

        container.volume_mounts.append(
            run_v2.VolumeMount(
                name=nfs_volume_name,
                mount_path=nfs_mount_path,
            )
        )

    job.template.template.containers.append(container)

    return job


def check_diff(remote_config: list[dict], local_config: list[dict]):
    """Return reconciliation actions by comparing configs by stable job name.

    jsondiff compares lists by position, so inserting a new job in the middle of
    the alphabetically sorted config can be reported as an update to the item at
    that index. For a brand-new Cloud Run job, that leads this script to call
    update_job before create_job, which fails with NOT_FOUND. Use job names as
    identifiers instead so new jobs are always inserted first.
    """

    result = {
        "inserted": None,
        "removed": None,
        "updated": None,
    }

    remote_by_name = {config_key(item): (index, item) for index, item in enumerate(remote_config)}
    local_by_name = {config_key(item): (index, item) for index, item in enumerate(local_config)}

    inserted = [
        (local_by_name[name][0], local_by_name[name][1])
        for name in sorted(set(local_by_name) - set(remote_by_name))
    ]
    removed = [
        (remote_by_name[name][0], remote_by_name[name][1])
        for name in sorted(set(remote_by_name) - set(local_by_name))
    ]
    updated = [
        (local_by_name[name][0], local_by_name[name][1])
        for name in sorted(set(remote_by_name) & set(local_by_name))
        if remote_by_name[name][1] != local_by_name[name][1]
    ]

    if inserted:
        result["inserted"] = inserted
    if removed:
        result["removed"] = removed
    if updated:
        result["updated"] = updated

    return result


def load_json(file_path):
    try:
        with open(file_path, "r") as file:
            return json.load(file)
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return None
    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from '{file_path}': {e}")
        return None


if __name__ == "__main__":
    main()
