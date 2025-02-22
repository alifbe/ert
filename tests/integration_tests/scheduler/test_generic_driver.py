import signal

import pytest

from ert.scheduler.lsf_driver import LsfDriver
from ert.scheduler.openpbs_driver import OpenPBSDriver
from tests.utils import poll

from .conftest import mock_bin


@pytest.fixture(params=[LsfDriver, OpenPBSDriver])
def driver(request, pytestconfig, monkeypatch, tmp_path):
    class_ = request.param

    # It's not possible to dynamically choose a pytest fixture in a fixture, so
    # we copy some code here
    if class_ is OpenPBSDriver and pytestconfig.getoption("openpbs"):
        # User provided --openpbs, which means we should use the actual OpenPBS
        # cluster without mocking anything.
        pass
    elif class_ is LsfDriver and pytestconfig.getoption("lsf"):
        # User provided --lsf, which means we should use the actual LSF
        # cluster without mocking anything.""
        pass
    else:
        mock_bin(monkeypatch, tmp_path)

    return class_()


@pytest.mark.integration_test
async def test_submit(driver, tmp_path):
    await driver.submit(0, f"echo test > {tmp_path}/test")
    await poll(driver, {0})

    assert (tmp_path / "test").read_text(encoding="utf-8") == "test\n"


async def test_submit_something_that_fails(driver):
    finished_called = False

    expected_returncode = 42
    if isinstance(driver, LsfDriver):
        expected_returncode = 1

    async def finished(iens, returncode, aborted):
        assert iens == 0
        assert returncode == expected_returncode

        if isinstance(driver, LsfDriver):
            assert aborted is True

        nonlocal finished_called
        finished_called = True

    await driver.submit(0, f"exit {expected_returncode}")
    await poll(driver, {0}, finished=finished)

    assert finished_called


async def test_kill(driver):
    aborted_called = False

    expected_returncode = 1
    if isinstance(driver, OpenPBSDriver):
        expected_returncode = 256 + signal.SIGTERM

    async def started(iens):
        nonlocal driver
        await driver.kill(iens)

    async def finished(iens, returncode, aborted):
        assert iens == 0
        assert returncode == expected_returncode
        assert aborted is True

        nonlocal aborted_called
        aborted_called = True

    await driver.submit(0, "sleep 3; exit 2")
    await poll(driver, {0}, started=started, finished=finished)
    assert aborted_called
