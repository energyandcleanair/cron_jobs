"""Tests for the Cloud Run job manager.

These tests exercise the ownership/marking logic without touching GCP: the
google client objects are replaced with in-memory fakes that record the
requests they receive.
"""
from types import SimpleNamespace

import pytest
from google.cloud import run_v2

import update_cloud_run as ucr


# --------------------------------------------------------------------------- #
# Fixtures / fakes
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """The script sleeps 2s between API calls; skip that in tests."""
    monkeypatch.setattr(ucr, "sleep", lambda *_: None)


def make_remote_job(name, *, managed=True, image="img", command="do thing", schedule_cmd=None):
    """Build a realistic run_v2.Job as returned by list_jobs."""
    job = run_v2.Job()
    job.name = ucr.full_project_job_name.format(job_name=name)
    if managed:
        job.annotations = {ucr.MANAGED_BY_KEY: ucr.MANAGED_BY_VALUE}
    job.template = run_v2.ExecutionTemplate()
    job.template.template = run_v2.TaskTemplate()
    job.template.parallelism = 1
    job.template.task_count = 1
    job.template.template.max_retries = 3
    job.template.template.timeout = {"seconds": 600}
    container = run_v2.Container()
    container.image = image
    container.resources.limits = {"cpu": "1", "memory": "512Mi"}
    container.args = ["/app/config/run.sh"] + command.split(" ")
    job.template.template.containers.append(container)
    return job


class FakeResponse:
    def __init__(self, name):
        self._name = name

    def result(self):
        return SimpleNamespace(name=self._name)


class FakeRunClient:
    def __init__(self):
        self.created, self.updated, self.deleted = [], [], []

    def create_job(self, request):
        self.created.append(request.job_id)
        return FakeResponse(request.job_id)

    def update_job(self, request):
        name = request.job.name.split("/")[-1]
        self.updated.append(name)
        return FakeResponse(name)

    def delete_job(self, request):
        name = request.name.split("/")[-1]
        self.deleted.append(name)
        return FakeResponse(name)


class FakeSchedulerClient:
    def __init__(self):
        self.created, self.updated, self.deleted = [], [], []

    def create_job(self, request):
        self.created.append(request.job.name.split("/")[-1])

    def update_job(self, request):
        self.updated.append(request.job.name.split("/")[-1])

    def delete_job(self, request):
        self.deleted.append(request.name.split("/")[-1])


# --------------------------------------------------------------------------- #
# Marking / filtering
# --------------------------------------------------------------------------- #
def test_is_managed_distinguishes_label():
    assert ucr.is_managed(make_remote_job("a", managed=True))
    assert not ucr.is_managed(make_remote_job("a", managed=False))


def test_is_managed_ignores_other_annotation_values():
    job = make_remote_job("a", managed=False)
    job.annotations = {ucr.MANAGED_BY_KEY: "someone-else"}
    assert not ucr.is_managed(job)


def test_filter_managed_jobs_drops_unmanaged():
    jobs = {
        "scrape-mee": make_remote_job("scrape-mee", managed=True),
        "episode-warm": make_remote_job("episode-warm", managed=False),
    }
    managed = ucr.filter_managed_jobs(jobs)
    assert set(managed) == {"scrape-mee"}


def test_construct_job_stamps_ownership_annotation():
    item = _item("scrape-mee")
    job = ucr.construct_job(item)
    assert dict(job.annotations) == {ucr.MANAGED_BY_KEY: ucr.MANAGED_BY_VALUE}
    # and the resulting job round-trips as "managed"
    assert ucr.is_managed(job)


# --------------------------------------------------------------------------- #
# The original bug: an unmanaged remote job must never reach the diff/removal
# --------------------------------------------------------------------------- #
def test_unmanaged_job_excluded_from_config_and_diff():
    jobs = {
        "scrape-mee": make_remote_job("scrape-mee"),
        "episode-warm": make_remote_job("episode-warm", managed=False),
    }
    remote = ucr.generate_current_config(ucr.filter_managed_jobs(jobs), schedulers={})
    names = [r["name"] for r in remote]
    assert "episode-warm" not in names

    # local config intentionally omits episode-warm (it is not ours)
    local = [r for r in remote]  # identical -> nothing to do
    diff = ucr.check_diff(remote, local)
    assert diff["removed"] is None


# --------------------------------------------------------------------------- #
# Reconciliation actions create the right resources
# --------------------------------------------------------------------------- #
def test_create_job_inserts_new_job():
    client = FakeRunClient()
    ucr.create_job(client, "inserted", _item("new-job"))
    assert client.created == ["new-job"]
    assert client.updated == []


def test_scheduler_inserted_for_new_job():
    client = FakeSchedulerClient()
    ucr.create_scheduler_job(client, "inserted", prev_item={}, item=_item("scrape-mee"))
    assert client.created == ["scrape-mee"]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _item(name, schedule="0 * * * *"):
    return {
        "name": name,
        "schedule": schedule,
        "time_zone": "UTC",
        "image": "img",
        "command": "do thing",
        "parallelism": 1,
        "taskCount": 1,
        "maxRetries": 3,
        "timeoutSeconds": 600,
        "cpu": "1",
        "memory": "512Mi",
        "nfs": False,
    }
