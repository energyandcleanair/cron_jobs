# cron_jobs

Configuration and deployment automation for scheduled data-processing jobs running on Google Cloud Run.

The repository defines the jobs, their schedules, execution parameters, and pipeline configurations. GitHub Actions deploys changes to Google Cloud, keeping managed Cloud Run Jobs and Cloud Scheduler jobs aligned with the repository configuration.

## Repository structure

| Path | Purpose |
|---|---|
| [`_config/cloud_run_config.json`](https://github.com/energyandcleanair/cron_jobs/blob/main/_config/cloud_run_config.json) | Central registry of managed Cloud Run jobs, including schedules, container images, resources, and execution parameters. |
| [`_config/update_cloud_run.py`](https://github.com/energyandcleanair/cron_jobs/blob/main/_config/update_cloud_run.py) | Reconciles job definitions with deployed Cloud Run and Cloud Scheduler resources. |
| [`_config/test_update_cloud_run.py`](https://github.com/energyandcleanair/cron_jobs/blob/main/_config/test_update_cloud_run.py) | Unit tests for deployment and reconciliation logic. |
| [`_config/requirements.txt`](https://github.com/energyandcleanair/cron_jobs/blob/main/_config/requirements.txt) | Python dependencies for deployment automation. |
| [`.github/workflows/`](https://github.com/energyandcleanair/cron_jobs/tree/main/.github/workflows) | GitHub Actions workflows for testing and deployment. |
| `scraping/`, `weather/`, `power/`, `plotting/`, `data_tests/`, etc. | Pipeline-specific configuration files referenced by jobs. |
| `debug/` | Configurations for manual execution and testing. |

## Managing jobs

### Adding or updating a job

Cloud Run jobs are defined in [`_config/cloud_run_config.json`](https://github.com/energyandcleanair/cron_jobs/blob/main/_config/cloud_run_config.json).

Each definition specifies:

- **Identity and scheduling:** Job name, cron schedule, and time zone.
- **Execution:** Container image, command, and arguments.
- **Resources:** CPU, memory, parallelism, task count, timeout, and retry limits.
- **Optional configuration:** Region, Secret Manager secrets, VPC access, and NFS mounts.

To add or modify a job:

1. Add or update its definition in `_config/cloud_run_config.json`, following the existing schema.
2. Create or update any associated pipeline configuration files.
3. Commit and merge the changes into `main`.
4. Verify the deployment in [GitHub Actions](https://github.com/energyandcleanair/cron_jobs/actions).

To remove a managed job, delete its entry from `cloud_run_config.json`. The reconciliation process removes the corresponding Cloud Run and Cloud Scheduler resources.

### Pipeline configurations

Domain directories contain configuration files consumed by individual jobs. These are typically referenced through the job's `args` field.

For example, a CREA engine job can reference a configuration hosted in this repository:

```json
{
  "command": "python3",
  "args": "main.py -u https://github.com/energyandcleanair/cron_jobs/raw/main/scraping/config_airnow.json"
}
```

Changes to these pipeline configurations do not themselves trigger the deployment workflow. Jobs referencing configurations on `main` can load the updated configuration when they execute.

## Deployment

Production deployment is managed by [`.github/workflows/cloud-run-workflow.yml`](https://github.com/energyandcleanair/cron_jobs/blob/main/.github/workflows/cloud-run-workflow.yml).

The workflow runs on pushes to `main` that modify files in `_config/` covered by its path filters, or the deployment workflow itself. It can also be started manually using `workflow_dispatch`.

The workflow:

1. Authenticates with GCP using Workload Identity Federation.
2. Installs dependencies from `_config/requirements.txt`.
3. Runs the Python test suite.
4. Executes `_config/update_cloud_run.py` to reconcile deployed resources.

### Resource reconciliation

`update_cloud_run.py` compares the repository's job definitions with the deployed configuration and creates, updates, or deletes resources as required.

**Ownership is identified by the Cloud Run annotation:**

```
energyandcleanair.org/managed-by=cron-jobs-config
```

Only jobs carrying this annotation are included in reconciliation. Other Cloud Run jobs remain outside the repository's management.

Managed jobs should therefore be modified through the repository to keep deployed resources consistent with their definitions.

## Development and testing

Changes to the deployment logic should be made in `_config/update_cloud_run.py`, with corresponding tests in `_config/test_update_cloud_run.py`.

Run the tests locally with:

```bash
cd _config
python -m pip install -r requirements.txt
python -m pytest -q
```

The [`tests.yml`](https://github.com/energyandcleanair/cron_jobs/blob/main/.github/workflows/tests.yml) workflow runs tests on relevant pull requests and non-`main` branch pushes.

The deployment workflow also runs the tests before applying changes to GCP.

## Manual execution

The `run-manual` Cloud Run job supports ad hoc execution using a configuration stored at:

```
debug/config_manual.json
```

Update this file with the required configuration, then execute the `run-manual` job in Cloud Run.

This provides a way to run individual configurations independently of their production schedules.
