import json
import jsondiff
from jsondiff import symbols
from google.cloud import run_v2, scheduler_v1
from time import sleep
from typing import Dict

run_client = run_v2.JobsClient()
scheduler_client = scheduler_v1.CloudSchedulerClient()
local_config_file_name = "cloud_run_config.json"

location = "europe-west1"
project_id = "crea-aq-data"
project_path = f"projects/{project_id}/locations/{location}"
full_project_job_name = project_path + "/jobs/{job_name}"
nsf_connector = project_path + "/connectors/nfs-connector"
service_account_email = "829505003332-compute@developer.gserviceaccount.com"
scheduler_endpoint = (
    f"https://{location}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{project_id}" + "/jobs/{job_name}:run"
)

nfs_volume_name = "crea-nfs"
nfs_path = "/crea_nfs"
nfs_mount_path = f"/mnt{nfs_path}"
nfs_server = "10.21.193.2"


def main():
    jobs, schedulers = get_remote_jobs_and_schedulers()
    remote_config = generate_current_config(jobs, schedulers)
    local_config = load_json(local_config_file_name)

    remote_config.sort(key=lambda obj: obj["name"])
    local_config.sort(key=lambda obj: obj["name"])

    diff = check_diff(remote_config, local_config)

    for action in ["inserted", "updated", "removed"]:
        if diff[action]:
            for k, item in diff[action]:
                prev_item = next((x for x in remote_config if x["name"] == item["name"]), {})
                create_job(action, item)
                create_scheduler_job(action, prev_item, item)

    print(diff)


def get_remote_jobs_and_schedulers():
    jobs_response = run_client.list_jobs(request=run_v2.ListJobsRequest(parent=project_path, page_size=500))
    schedulers_response = scheduler_client.list_jobs(
        request=scheduler_v1.ListJobsRequest(parent=project_path, page_size=500)
    )

    jobs = {job.name.split("/")[-1]: job for job in jobs_response}
    schedulers = {scheduler.name.split("/")[-1]: scheduler for scheduler in schedulers_response}

    print(f"Found {len(jobs)} jobs and {len(schedulers)} schedulers.")
    return jobs, schedulers


def generate_current_config(jobs: Dict[str, run_v2.Job], schedulers: Dict[str, scheduler_v1.Job]):
    output = []

    # join jobs and schedulers based on name
    for job_name, job in jobs.items():
        scheduler = schedulers.get(job_name, None)

        uses_nfs = job.template.template.vpc_access.connector not in [None, ""]
        # skip first command arg if nfs is used
        index_args = 1 if uses_nfs else 0
        command = " ".join(job.template.template.containers[0].args[index_args:])

        output.append(
            {
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
        )

    return output


def create_job(action, item):
    if action == "inserted":
        job = construct_job(item)
        job.name = None
        create_request = run_v2.CreateJobRequest(job=job, parent=project_path, job_id=item["name"])
        response = run_client.create_job(request=create_request)
        action_msg = "Created job"

    elif action == "updated":
        update_request = run_v2.UpdateJobRequest(job=construct_job(item))
        response = run_client.update_job(request=update_request)
        action_msg = "Updated job"

    elif action == "removed":
        delete_request = run_v2.DeleteJobRequest(name=full_project_job_name.format(job_name=item["name"]))
        response = run_client.delete_job(request=delete_request)
        action_msg = "Deleted job"

    print(f"{action_msg}: {response.result().name}")
    sleep(2)


def create_scheduler_job(action: str, prev_item: dict, item: dict):

    if not prev_item:
        action = "inserted"
    elif prev_item["schedule"] and not item["schedule"]:
        action = "removed"
    elif prev_item["schedule"] != item["schedule"]:
        action = "updated"

    if action == "inserted":
        job = construct_scheduler(item)
        create_request = scheduler_v1.CreateJobRequest(parent=project_path, job=job)
        scheduler_client.create_job(request=create_request)
        action_msg = "Created scheduler job"

    elif action == "updated":
        update_request = scheduler_v1.UpdateJobRequest(job=construct_scheduler(item))
        scheduler_client.update_job(request=update_request)
        action_msg = "Updated scheduler job"

    elif action == "removed":
        delete_request = scheduler_v1.DeleteJobRequest(name=full_project_job_name.format(job_name=item["name"]))
        scheduler_client.delete_job(request=delete_request)
        action_msg = "Deleted scheduler job"

    print(f"{action_msg}: {item['name']}")
    sleep(2)


def construct_scheduler(item: dict):
    job = scheduler_v1.Job()
    job.name = full_project_job_name.format(job_name=item["name"])
    job.schedule = item["schedule"]
    job.time_zone = item["time_zone"]
    job.http_target = scheduler_v1.HttpTarget()
    job.http_target.uri = scheduler_endpoint.format(job_name=item["name"])
    job.http_target.http_method = scheduler_v1.HttpMethod.POST
    job.http_target.oauth_token.service_account_email = service_account_email
    return job


def construct_job(item: dict):
    job = run_v2.Job()
    job.name = full_project_job_name.format(job_name=item["name"])
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
        vpc.connector = nsf_connector
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


def check_diff(remote_config: dict, local_config: dict):
    diff = jsondiff.diff(remote_config, local_config, syntax="explicit")

    result = {
        "inserted": None,
        "removed": None,
        "updated": None,
    }

    if diff == {} or diff is None:  # If no differences found
        return result

    if symbols.insert in diff:
        result["inserted"] = diff[symbols.insert]

    if symbols.delete in diff:
        result["removed"] = [(key, remote_config[key]) for key in diff[symbols.delete]]

    # Check for completely removed items in the current data
    if diff == [] and len(remote_config) > 0 and len(local_config) == 0:
        result["removed"] = [(key, remote_config[key]) for key, _ in enumerate(remote_config)]
        return result

    result["updated"] = [(key, local_config[key]) for key, _ in diff.items() if isinstance(key, int)]

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
