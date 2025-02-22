import asyncio
import json
import random
import shutil
import time
from pathlib import Path
from typing import List

import pytest
from cloudevents.http import from_json

from ert.constant_filenames import CERT_FILE
from ert.ensemble_evaluator._builder._realization import Realization
from ert.job_queue.queue import EVTYPE_ENSEMBLE_CANCELLED, EVTYPE_ENSEMBLE_STOPPED
from ert.run_arg import RunArg
from ert.scheduler import scheduler


def create_jobs_json(realization: Realization) -> None:
    jobs = {
        "global_environment": {},
        "config_path": "/dev/null",
        "config_file": "/dev/null",
        "jobList": [
            {
                "name": forward_model.name,
                "executable": forward_model.executable,
                "argList": forward_model.arglist,
            }
            for forward_model in realization.forward_models
        ],
        "run_id": "0",
        "ert_pid": "0",
        "real_id": str(realization.iens),
    }
    realization_run_path = Path(realization.run_arg.runpath)
    realization_run_path.mkdir()
    with open(realization_run_path / "jobs.json", mode="w", encoding="utf-8") as f:
        json.dump(jobs, f)


@pytest.fixture
def realization(storage, tmp_path):
    ensemble = storage.create_experiment().create_ensemble(name="foo", ensemble_size=1)
    return create_stub_realization(ensemble, tmp_path, 0)


def create_stub_realization(ensemble, base_path: Path, iens) -> Realization:
    run_arg = RunArg(
        run_id="",
        ensemble_storage=ensemble,
        iens=iens,
        itr=0,
        runpath=str(base_path / f"realization-{iens}"),
        job_name="",
    )

    realization = Realization(
        iens=iens,
        forward_models=[],
        active=True,
        max_runtime=None,
        run_arg=run_arg,
        num_cpu=1,
        job_script=str(shutil.which("job_dispatch.py")),
    )
    return realization


async def test_empty(mock_driver):
    sch = scheduler.Scheduler(mock_driver())
    assert await sch.execute() == EVTYPE_ENSEMBLE_STOPPED


async def test_single_job(realization, mock_driver):
    future = asyncio.Future()

    async def init(iens, *args, **kwargs):
        future.set_result(iens)

    driver = mock_driver(init=init)

    sch = scheduler.Scheduler(driver, [realization])

    assert await sch.execute() == EVTYPE_ENSEMBLE_STOPPED
    assert await future == realization.iens


async def test_cancel(realization, mock_driver):
    pre = asyncio.Event()
    post = asyncio.Event()
    killed = False

    async def wait():
        pre.set()
        await asyncio.sleep(10)
        post.set()

    async def kill():
        nonlocal killed
        killed = True

    driver = mock_driver(wait=wait, kill=kill)
    sch = scheduler.Scheduler(driver, [realization])

    scheduler_task = asyncio.create_task(sch.execute())

    # Wait for the job to start
    await asyncio.wait_for(pre.wait(), timeout=1)

    # Kill all jobs and wait for the scheduler to complete
    await sch.cancel_all_jobs()  # this is equivalent to sch.kill_all_jobs()
    await scheduler_task

    assert pre.is_set()
    assert not post.is_set()
    assert killed


async def test_add_dispatch_information_to_jobs_file(
    storage, tmp_path: Path, mock_driver
):
    test_ee_uri = "ws://test_ee_uri.com/121/"
    test_ens_id = "test_ens_id121"
    test_ee_token = "test_ee_token_t0k€n121"
    test_ee_cert = "test_ee_cert121.pem"

    ensemble_size = 10

    ensemble = storage.create_experiment().create_ensemble(
        name="foo", ensemble_size=ensemble_size
    )
    realizations = [
        create_stub_realization(ensemble, tmp_path, iens)
        for iens in range(ensemble_size)
    ]

    sch = scheduler.Scheduler(
        mock_driver(),
        realizations=realizations,
        ens_id=test_ens_id,
        ee_uri=test_ee_uri,
        ee_cert=test_ee_cert,
        ee_token=test_ee_token,
    )

    for realization in realizations:
        create_jobs_json(realization)

    sch.add_dispatch_information_to_jobs_file()

    for realization in realizations:
        job_file_path = Path(realization.run_arg.runpath) / "jobs.json"
        cert_file_path = Path(realization.run_arg.runpath) / CERT_FILE
        content: dict = json.loads(job_file_path.read_text(encoding="utf-8"))
        assert content["ens_id"] == test_ens_id
        assert content["real_id"] == realization.iens
        assert content["dispatch_url"] == test_ee_uri
        assert content["ee_token"] == test_ee_token
        assert content["ee_cert_path"] == str(cert_file_path)
        assert type(content["jobList"]) == list and len(content["jobList"]) == 0
        assert cert_file_path.read_text(encoding="utf-8") == test_ee_cert


@pytest.mark.parametrize("max_submit", [1, 2, 3])
async def test_that_max_submit_was_reached(realization, max_submit, mock_driver):
    retries = 0

    async def init(*args, **kwargs):
        nonlocal retries
        retries += 1

    async def wait():
        return 1

    driver = mock_driver(init=init, wait=wait)
    sch = scheduler.Scheduler(driver, [realization])

    sch._max_submit = max_submit

    assert await sch.execute() == EVTYPE_ENSEMBLE_STOPPED
    assert retries == max_submit


async def test_that_max_submit_is_not_reached_on_success(realization, mock_driver):
    retries = 0

    async def init(*args, **kwargs):
        nonlocal retries
        retries += 1

    driver = mock_driver(init=init)
    sch = scheduler.Scheduler(driver, [realization], max_submit=5)

    assert await sch.execute() == EVTYPE_ENSEMBLE_STOPPED
    assert retries == 1


@pytest.mark.timeout(10)
async def test_max_runtime(realization, mock_driver, caplog):
    wait_started = asyncio.Event()

    async def wait():
        wait_started.set()
        await asyncio.sleep(100)

    realization.max_runtime = 1

    sch = scheduler.Scheduler(mock_driver(wait=wait), [realization])

    result = await asyncio.create_task(sch.execute())
    assert wait_started.is_set()
    assert result == EVTYPE_ENSEMBLE_STOPPED

    timeouteventfound = False
    while not timeouteventfound and not sch._events.empty():
        event = await sch._events.get()
        if from_json(event)["type"] == "com.equinor.ert.realization.timeout":
            timeouteventfound = True
    assert timeouteventfound

    assert "Realization 0 stopped due to MAX_RUNTIME=1 seconds" in caplog.text


async def test_no_resubmit_on_max_runtime_kill(realization, mock_driver):
    retries = 0

    async def init(*args, **kwargs):
        nonlocal retries
        retries += 1

    async def wait():
        await asyncio.sleep(100)

    realization.max_runtime = 1

    sch = scheduler.Scheduler(
        mock_driver(init=init, wait=wait), [realization], max_submit=2
    )
    result = await sch.execute()
    assert result == EVTYPE_ENSEMBLE_STOPPED

    assert retries == 1


@pytest.mark.parametrize("max_running", [0, 1, 2, 10])
async def test_max_running(max_running, mock_driver, storage, tmp_path):
    runs: List[bool] = []

    async def wait():
        nonlocal runs
        runs.append(True)
        await asyncio.sleep(0.01)
        runs.append(False)

    # Ensemble size must be larger than max_running to be able
    # to expose issues related to max_running
    ensemble_size = max_running * 3 if max_running > 0 else 10

    ensemble = storage.create_experiment().create_ensemble(
        name="foo", ensemble_size=ensemble_size
    )
    realizations = [
        create_stub_realization(ensemble, tmp_path, iens)
        for iens in range(ensemble_size)
    ]

    sch = scheduler.Scheduler(
        mock_driver(wait=wait), realizations, max_running=max_running
    )

    assert await sch.execute() == EVTYPE_ENSEMBLE_STOPPED

    currently_running = 0
    max_running_observed = 0
    for run in runs:
        currently_running += 1 if run else -1
        max_running_observed = max(max_running_observed, currently_running)

    if max_running > 0:
        assert max_running_observed == max_running
    else:
        assert max_running_observed == ensemble_size


@pytest.mark.timeout(6)
async def test_max_runtime_while_killing(realization, mock_driver):
    wait_started = asyncio.Event()
    now_kill_me = asyncio.Event()

    async def wait():
        # A realization function that lives forever if it was not killed
        wait_started.set()
        await asyncio.sleep(0.1)
        now_kill_me.set()
        await asyncio.sleep(1000)

    async def kill():
        # A kill function that is triggered before the timeout, but finishes
        # after MAX_RUNTIME
        await asyncio.sleep(1)

    realization.max_runtime = 1

    sch = scheduler.Scheduler(mock_driver(wait=wait, kill=kill), [realization])

    scheduler_task = asyncio.create_task(sch.execute())

    await now_kill_me.wait()
    await sch.cancel_all_jobs()  # this is equivalent to sch.kill_all_jobs()

    # Sleep until max_runtime must have kicked in:
    await asyncio.sleep(1.1)

    timeouteventfound = False
    while not timeouteventfound and not sch._events.empty():
        event = await sch._events.get()
        if from_json(event)["type"] == "com.equinor.ert.realization.timeout":
            timeouteventfound = True

    # Assert that a timeout_event is actually emitted, because killing took a
    # long time, and that we should exit normally (asserting no bad things
    # happen just because we have two things killing the realization).

    assert timeouteventfound
    await scheduler_task

    # The result from execute is that we were cancelled, not stopped
    # as if the timeout happened before kill_all_jobs()
    assert scheduler_task.result() == EVTYPE_ENSEMBLE_CANCELLED


async def test_is_active(mock_driver, realization):
    """The is_active() function is only used by simulation_context.py"""
    realization_started = asyncio.Event()

    async def init(iens, *args, **kwargs):
        realization_started.set()
        await asyncio.sleep(0.001)  # Ensure time to measure activeness

    sch = scheduler.Scheduler(mock_driver(init=init), [realization])

    execute_task = asyncio.create_task(sch.execute())
    assert not sch.is_active()
    await realization_started.wait()
    assert sch.is_active()
    await execute_task
    assert not sch.is_active()


@pytest.mark.timeout(6)
async def test_that_long_running_jobs_were_stopped(storage, tmp_path, mock_driver):
    killed_iens = []

    async def kill(iens):
        nonlocal killed_iens
        killed_iens.append(iens)

    async def wait(iens):
        # all jobs with iens > 5 will sleep for 10 seconds and should be killed
        if iens < 6:
            await asyncio.sleep(0.1)
        else:
            await asyncio.sleep(10)

    ensemble_size = 10
    ensemble = storage.create_experiment().create_ensemble(
        name="foo", ensemble_size=ensemble_size
    )
    realizations = [
        create_stub_realization(ensemble, tmp_path, iens)
        for iens in range(ensemble_size)
    ]

    sch = scheduler.Scheduler(
        mock_driver(wait=wait, kill=kill), realizations, max_running=ensemble_size
    )

    assert await sch.execute(min_required_realizations=5) == EVTYPE_ENSEMBLE_STOPPED
    assert killed_iens == [6, 7, 8, 9]


@pytest.mark.flaky(reruns=5)
@pytest.mark.parametrize(
    "submit_sleep, iens_stride, realization_runtime",
    [(0, 1, 0.1), (0.1, 1, 0.1), (0.1, 1, 0), (0.1, 2, 0)],
)
async def test_submit_sleep(
    submit_sleep,
    iens_stride,  # Gives sparse ensembles when > 1
    realization_runtime,
    storage,
    tmp_path,
    mock_driver,
):
    run_start_times: List[float] = []

    async def wait():
        nonlocal run_start_times
        run_start_times.append(time.time())
        await asyncio.sleep(realization_runtime)

    ensemble_size = 10

    ensemble = storage.create_experiment().create_ensemble(
        name="foo", ensemble_size=ensemble_size * iens_stride
    )
    realizations = [
        create_stub_realization(ensemble, tmp_path, iens * iens_stride)
        for iens in range(ensemble_size)
    ]

    sch = scheduler.Scheduler(
        mock_driver(wait=wait), realizations, submit_sleep=submit_sleep, max_running=0
    )
    await sch.execute()

    deltas = [
        next_start - start
        for start, next_start in zip(run_start_times[:-1], run_start_times[1:])
    ]
    assert min(deltas) >= submit_sleep * 0.8
    assert max(deltas) <= submit_sleep + 0.1


@pytest.mark.flaky(reruns=5)
@pytest.mark.parametrize(
    "submit_sleep, realization_max_runtime, max_running",
    [
        (0.15, 0.10, 1),
        (0.15, 0.10, 10),
        (0.15, 0.35, 5),
    ],
)
async def test_submit_sleep_with_max_running(
    submit_sleep, realization_max_runtime, max_running, storage, tmp_path, mock_driver
):
    run_start_times: List[float] = []

    async def wait():
        nonlocal run_start_times
        run_start_times.append(time.time())
        # If the realization runtimes are constant, we will never get into
        # the situation where we can start many realizations at the same moment
        runtime = realization_max_runtime * random.random()
        await asyncio.sleep(runtime)

    ensemble_size = 10

    ensemble = storage.create_experiment().create_ensemble(
        name="foo", ensemble_size=ensemble_size
    )
    realizations = [
        create_stub_realization(ensemble, tmp_path, iens)
        for iens in range(ensemble_size)
    ]

    sch = scheduler.Scheduler(
        mock_driver(wait=wait),
        realizations,
        submit_sleep=submit_sleep,
        max_running=max_running,
    )
    await sch.execute()

    deltas = [
        next_start - start
        for start, next_start in zip(run_start_times[:-1], run_start_times[1:])
    ]
    assert min(deltas) >= submit_sleep * 0.8
