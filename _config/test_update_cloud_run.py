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


def make_remote_job(
    name,
    *,
    managed=True,
    image="img",
    command="do thing",
    schedule_cmd=None,
    region=ucr.default_location,
):
    """Build a realistic run_v2.Job as returned by list_jobs."""
    job = run_v2.Job()
    job.name = ucr.full_project_job_name_for(region, name)
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


def test_construct_job_uses_configured_region():
    item = _item("scrape-cpcb", region="asia-south1")
    job = ucr.construct_job(item)
    assert job.name == "projects/crea-aq-data/locations/asia-south1/jobs/scrape-cpcb"


def test_construct_scheduler_uses_configured_region():
    item = _item("scrape-cpcb", region="asia-south1")
    job = ucr.construct_scheduler(item)
    assert job.name == "projects/crea-aq-data/locations/asia-south1/jobs/scrape-cpcb"
    assert job.http_target.uri == (
        "https://asia-south1-run.googleapis.com/apis/run.googleapis.com/v1/"
        "namespaces/crea-aq-data/jobs/scrape-cpcb:run"
    )


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


def test_check_diff_treats_middle_insertion_as_inserted_not_updated():
    remote = [_item("a-job"), _item("c-job")]
    local = [_item("a-job"), _item("b-job"), _item("c-job")]

    diff = ucr.check_diff(remote, local)

    assert diff["inserted"] == [(1, _item("b-job"))]
    assert diff["updated"] is None
    assert diff["removed"] is None


def test_check_diff_detects_updates_by_name_after_insertion():
    remote = [_item("a-job"), _item("c-job", schedule="0 * * * *")]
    local = [_item("a-job"), _item("b-job"), _item("c-job", schedule="30 * * * *")]

    diff = ucr.check_diff(remote, local)

    assert diff["inserted"] == [(1, _item("b-job"))]
    assert diff["updated"] == [(2, _item("c-job", schedule="30 * * * *"))]
    assert diff["removed"] is None


def test_check_diff_treats_rename_as_insert_and_remove():
    remote = [_item("a-job"), _item("old-job"), _item("z-job")]
    local = [_item("a-job"), _item("new-job"), _item("z-job")]

    diff = ucr.check_diff(remote, local)

    assert diff["inserted"] == [(1, _item("new-job"))]
    assert diff["removed"] == [(1, _item("old-job"))]
    assert diff["updated"] is None


def test_check_diff_treats_region_change_as_insert_and_remove():
    remote = [_item("scrape-cpcb")]
    local = [_item("scrape-cpcb", region="asia-south1")]

    diff = ucr.check_diff(remote, local)

    assert diff["inserted"] == [(0, _item("scrape-cpcb", region="asia-south1"))]
    assert diff["removed"] == [(0, _item("scrape-cpcb"))]
    assert diff["updated"] is None


def test_generate_current_config_includes_non_default_region():
    remote_job = make_remote_job("scrape-cpcb", region="asia-south1")
    remote = ucr.generate_current_config({ucr.job_key_from_resource(remote_job.name): remote_job}, schedulers={})
    assert remote[0]["region"] == "asia-south1"


def test_get_config_locations_includes_default_and_configured_regions():
    assert ucr.get_config_locations([_item("a-job"), _item("b-job", region="asia-south1")]) == [
        "asia-south1",
        "europe-west1",
    ]


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
def _item(name, schedule="0 * * * *", region=None):
    item = {
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
    if region:
        item["region"] = region
    return item
