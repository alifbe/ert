import contextlib
import copy
import fileinput
import os
import os.path
import shutil
import stat
import time
from datetime import datetime as dt
from textwrap import dedent
from typing import List, Type, TypeVar
from unittest.mock import MagicMock, Mock

import pytest
from pytestqt.qtbot import QtBot
from qtpy.QtCore import Qt, QTimer
from qtpy.QtWidgets import QApplication, QComboBox, QMessageBox, QPushButton, QWidget

from ert.config import ErtConfig
from ert.enkf_main import EnKFMain
from ert.ensemble_evaluator.snapshot import (
    ForwardModel,
    RealizationSnapshot,
    Snapshot,
    SnapshotBuilder,
    SnapshotDict,
)
from ert.ensemble_evaluator.state import (
    ENSEMBLE_STATE_STARTED,
    FORWARD_MODEL_STATE_START,
    REALIZATION_STATE_UNKNOWN,
)
from ert.gui.ertwidgets import ClosableDialog
from ert.gui.ertwidgets.caselist import AddWidget
from ert.gui.ertwidgets.caseselector import CaseSelector
from ert.gui.ertwidgets.create_experiment_dialog import CreateExperimentDialog
from ert.gui.ertwidgets.storage_widget import StorageWidget
from ert.gui.main import ErtMainWindow, GUILogHandler, _setup_main_window
from ert.gui.simulation.ensemble_experiment_panel import EnsembleExperimentPanel
from ert.gui.simulation.run_dialog import RunDialog
from ert.gui.simulation.simulation_panel import SimulationPanel
from ert.gui.simulation.view import RealizationWidget
from ert.gui.tools.load_results.load_results_panel import LoadResultsPanel
from ert.gui.tools.manage_cases.case_init_configuration import (
    CaseInitializationConfigurationPanel,
)
from ert.run_models import EnsembleExperiment, MultipleDataAssimilation
from ert.services import StorageService
from ert.storage import open_storage
from tests.unit_tests.gui.simulation.test_run_path_dialog import handle_run_path_dialog


def with_manage_tool(gui, qtbot: QtBot, callback) -> None:
    def handle_manage_dialog():
        dialog = wait_for_child(gui, qtbot, ClosableDialog, name="manage-cases")
        cases_panel = get_child(dialog, CaseInitializationConfigurationPanel)
        callback(dialog, cases_panel)

    QTimer.singleShot(1000, handle_manage_dialog)
    manage_tool = gui.tools["Manage cases"]
    manage_tool.trigger()


@pytest.mark.usefixtures("use_tmpdir")
@pytest.fixture(name="opened_main_window", scope="module")
def opened_main_window_fixture(source_root, tmpdir_factory) -> ErtMainWindow:
    with pytest.MonkeyPatch.context() as mp:
        tmp_path = tmpdir_factory.mktemp("test-data")
        shutil.copytree(
            os.path.join(source_root, "test-data", "poly_example"),
            tmp_path / "test_data",
        )
        mp.chdir(tmp_path / "test_data")
        with fileinput.input("poly.ert", inplace=True) as fin:
            for line in fin:
                if "NUM_REALIZATIONS" in line:
                    # Decrease the number of realizations to speed up the test,
                    # if there is flakyness, this can be increased.
                    print("NUM_REALIZATIONS 20", end="\n")
                else:
                    print(line, end="")
            config = ErtConfig.from_file("poly.ert")
            poly_case = EnKFMain(config)
        args_mock = Mock()
        args_mock.config = "poly.ert"

        with StorageService.init_service(
            project=os.path.abspath(config.ens_path),
        ), open_storage(config.ens_path, mode="w") as storage:
            gui = _setup_main_window(poly_case, args_mock, GUILogHandler(), storage)
            gui.notifier.set_current_case(
                storage.create_experiment(
                    parameters=config.ensemble_config.parameter_configuration,
                    observations=config.observations,
                ).create_ensemble(
                    name="default",
                    ensemble_size=config.model_config.num_realizations,
                )
            )
            yield gui
            gui.close()


@pytest.fixture
def opened_main_window_clean(source_root, tmpdir):
    with pytest.MonkeyPatch.context() as mp:
        shutil.copytree(
            os.path.join(source_root, "test-data", "poly_example"),
            tmpdir / "test_data",
        )
        mp.chdir(tmpdir / "test_data")

        with fileinput.input("poly.ert", inplace=True) as fin:
            for line in fin:
                if "NUM_REALIZATIONS" in line:
                    # Decrease the number of realizations to speed up the test,
                    # if there is flakyness, this can be increased.
                    print("NUM_REALIZATIONS 20", end="\n")
                else:
                    print(line, end="")

        poly_case = EnKFMain(ErtConfig.from_file("poly.ert"))
        args_mock = Mock()
        args_mock.config = "poly.ert"

        with StorageService.init_service(
            project=os.path.abspath(poly_case.ert_config.ens_path),
        ), open_storage(poly_case.ert_config.ens_path, mode="w") as storage:
            gui = _setup_main_window(poly_case, args_mock, GUILogHandler(), storage)
            yield gui


@pytest.mark.usefixtures("use_tmpdir, opened_main_window")
@pytest.fixture(scope="module")
def esmda_has_run(run_experiment):
    # Runs a default ES-MDA run
    run_experiment(MultipleDataAssimilation)


@pytest.mark.usefixtures("use_tmpdir")
@pytest.fixture(name="run_experiment", scope="module")
def run_experiment_fixture(request, opened_main_window):
    def func(experiment_mode):
        qtbot = QtBot(request)
        gui = opened_main_window
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree("poly_out")
        # Select correct experiment in the simulation panel
        simulation_panel = gui.findChild(SimulationPanel)
        assert isinstance(simulation_panel, SimulationPanel)
        simulation_mode_combo = simulation_panel.findChild(QComboBox)
        assert isinstance(simulation_mode_combo, QComboBox)
        simulation_mode_combo.setCurrentText(experiment_mode.name())

        # Click start simulation and agree to the message
        start_simulation = simulation_panel.findChild(QWidget, name="start_simulation")

        def handle_dialog():
            qtbot.mouseClick(
                wait_for_child(gui, qtbot, QMessageBox).buttons()[0], Qt.LeftButton
            )

            QTimer.singleShot(
                500, lambda: handle_run_path_dialog(gui, qtbot, delete_run_path=False)
            )

        if not experiment_mode.name() in ("Ensemble experiment", "Evaluate ensemble"):
            QTimer.singleShot(500, handle_dialog)
        qtbot.mouseClick(start_simulation, Qt.LeftButton)

        # The Run dialog opens, click show details and wait until done appears
        # then click it
        qtbot.waitUntil(lambda: gui.findChild(RunDialog) is not None)
        run_dialog = gui.findChild(RunDialog)

        qtbot.mouseClick(run_dialog.show_details_button, Qt.LeftButton)

        qtbot.waitUntil(run_dialog.done_button.isVisible, timeout=200000)
        qtbot.waitUntil(lambda: run_dialog._tab_widget.currentWidget() is not None)

        # Assert that the number of boxes in the detailed view is
        # equal to the number of realizations
        realization_widget = run_dialog._tab_widget.currentWidget()
        assert isinstance(realization_widget, RealizationWidget)
        list_model = realization_widget._real_view.model()
        assert (
            list_model.rowCount()
            == simulation_panel.ert.ert_config.model_config.num_realizations
        )

        qtbot.mouseClick(run_dialog.done_button, Qt.LeftButton)

    return func


@pytest.mark.usefixtures("use_tmpdir")
@pytest.fixture(scope="module")
def ensemble_experiment_has_run(opened_main_window, run_experiment, request):
    gui = opened_main_window

    simulation_panel = get_child(gui, SimulationPanel)
    simulation_mode_combo = get_child(simulation_panel, QComboBox)
    simulation_settings = get_child(simulation_panel, EnsembleExperimentPanel)
    simulation_mode_combo.setCurrentText(EnsembleExperiment.name())

    simulation_settings._ensemble_name_field.setText("iter-0")

    with open("poly_eval.py", "w", encoding="utf-8") as f:
        f.write(
            dedent(
                """\
                #!/usr/bin/env python
                import numpy as np
                import sys
                import json

                def _load_coeffs(filename):
                    with open(filename, encoding="utf-8") as f:
                        return json.load(f)["COEFFS"]

                def _evaluate(coeffs, x):
                    return coeffs["a"] * x**2 + coeffs["b"] * x + coeffs["c"]

                if __name__ == "__main__":
                    if np.random.random(1) > 0.5:
                        sys.exit(1)
                    coeffs = _load_coeffs("parameters.json")
                    output = [_evaluate(coeffs, x) for x in range(10)]
                    with open("poly.out", "w", encoding="utf-8") as f:
                        f.write("\\n".join(map(str, output)))
                """
            )
        )
    os.chmod(
        "poly_eval.py",
        os.stat("poly_eval.py").st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH,
    )
    run_experiment(EnsembleExperiment)


@pytest.fixture()
def full_snapshot() -> Snapshot:
    real = RealizationSnapshot(
        status=REALIZATION_STATE_UNKNOWN,
        active=True,
        forward_models={
            "0": ForwardModel(
                start_time=dt.now(),
                end_time=dt.now(),
                name="poly_eval",
                index="0",
                status=FORWARD_MODEL_STATE_START,
                error="error",
                stdout="std_out_file",
                stderr="std_err_file",
                current_memory_usage="123",
                max_memory_usage="312",
            ),
            "1": ForwardModel(
                start_time=dt.now(),
                end_time=dt.now(),
                name="poly_postval",
                index="1",
                status=FORWARD_MODEL_STATE_START,
                error="error",
                stdout="std_out_file",
                stderr="std_err_file",
                current_memory_usage="123",
                max_memory_usage="312",
            ),
            "2": ForwardModel(
                start_time=dt.now(),
                end_time=None,
                name="poly_post_mortem",
                index="2",
                status=FORWARD_MODEL_STATE_START,
                error="error",
                stdout="std_out_file",
                stderr="std_err_file",
                current_memory_usage="123",
                max_memory_usage="312",
            ),
        },
    )
    snapshot = SnapshotDict(
        status=ENSEMBLE_STATE_STARTED,
        reals={},
    )
    for i in range(0, 100):
        snapshot.reals[str(i)] = copy.deepcopy(real)

    return Snapshot(snapshot.model_dump())


@pytest.fixture()
def large_snapshot() -> Snapshot:
    builder = SnapshotBuilder()
    for i in range(0, 150):
        builder.add_forward_model(
            forward_model_id=str(i),
            index=str(i),
            name=f"job_{i}",
            current_memory_usage="500",
            max_memory_usage="1000",
            status=FORWARD_MODEL_STATE_START,
            stdout=f"job_{i}.stdout",
            stderr=f"job_{i}.stderr",
            start_time=dt(1999, 1, 1).isoformat(),
            end_time=dt(2019, 1, 1).isoformat(),
        )
    real_ids = [str(i) for i in range(0, 150)]
    return builder.build(real_ids, REALIZATION_STATE_UNKNOWN)


@pytest.fixture()
def small_snapshot() -> Snapshot:
    builder = SnapshotBuilder()
    for i in range(0, 2):
        builder.add_forward_model(
            forward_model_id=str(i),
            index=str(i),
            name=f"job_{i}",
            current_memory_usage="500",
            max_memory_usage="1000",
            status=FORWARD_MODEL_STATE_START,
            stdout=f"job_{i}.stdout",
            stderr=f"job_{i}.stderr",
            start_time=dt(1999, 1, 1).isoformat(),
            end_time=dt(2019, 1, 1).isoformat(),
        )
    real_ids = [str(i) for i in range(0, 5)]
    return builder.build(real_ids, REALIZATION_STATE_UNKNOWN)


@pytest.fixture(name="active_realizations")
def active_realizations_fixture() -> Mock:
    active_reals = MagicMock()
    active_reals.count = Mock(return_value=10)
    active_reals.__iter__.return_value = [True] * 10
    return active_reals


@pytest.fixture
def runmodel(active_realizations) -> Mock:
    brm = Mock()
    brm.get_runtime = Mock(return_value=100)
    brm.hasRunFailed = Mock(return_value=False)
    brm.getFailMessage = Mock(return_value="")
    brm.support_restart = True
    brm._simulation_arguments = {"active_realizations": active_realizations}
    brm.has_failed_realizations = lambda: False
    return brm


class MockTracker:
    def __init__(self, events) -> None:
        self._events = events
        self._is_running = True

    def track(self):
        for event in self._events:
            if not self._is_running:
                break
            time.sleep(0.1)
            yield event

    def reset(self):
        pass

    def request_termination(self):
        self._is_running = False


@pytest.fixture
def mock_tracker():
    def _make_mock_tracker(events):
        return MockTracker(events)

    return _make_mock_tracker


def load_results_manually(qtbot, gui, case_name="default"):
    def handle_load_results_dialog():
        dialog = wait_for_child(gui, qtbot, ClosableDialog)
        panel = get_child(dialog, LoadResultsPanel)

        case_selector = get_child(panel, CaseSelector)
        index = case_selector.findText(case_name, Qt.MatchFlag.MatchContains)
        assert index != -1
        case_selector.setCurrentIndex(index)

        # click on "Load"
        load_button = get_child(panel.parent(), QPushButton, name="Load")

        # Verify that the messagebox is the success kind
        def handle_popup_dialog():
            messagebox = QApplication.activeModalWidget()
            assert isinstance(messagebox, QMessageBox)
            assert messagebox.text() == "Successfully loaded all realisations"
            ok_button = messagebox.button(QMessageBox.Ok)
            qtbot.mouseClick(ok_button, Qt.LeftButton)

        QTimer.singleShot(2000, handle_popup_dialog)
        qtbot.mouseClick(load_button, Qt.LeftButton)
        dialog.close()

    QTimer.singleShot(1000, handle_load_results_dialog)
    load_results_tool = gui.tools["Load results manually"]
    load_results_tool.trigger()


def add_experiment_manually(
    qtbot, gui, experiment_name="My experiment", ensemble_name="default"
):
    def handle_dialog(dialog, experiments_panel):
        # Open the create new experiment tab
        experiments_panel.setCurrentIndex(0)
        current_tab = experiments_panel.currentWidget()
        assert current_tab.objectName() == "create_new_case_tab"
        storage_widget = get_child(current_tab, StorageWidget)
        add_widget = get_child(storage_widget, AddWidget)

        def handle_add_dialog():
            dialog = wait_for_child(gui, qtbot, CreateExperimentDialog)
            dialog._experiment_edit.setText(experiment_name)
            dialog._ensemble_edit.setText(ensemble_name)

            qtbot.mouseClick(dialog._ok_button, Qt.MouseButton.LeftButton)

        QTimer.singleShot(1000, handle_add_dialog)
        qtbot.mouseClick(add_widget.addButton, Qt.MouseButton.LeftButton)

        dialog.close()

    with_manage_tool(gui, qtbot, handle_dialog)


V = TypeVar("V")


def wait_for_child(gui, qtbot: QtBot, typ: Type[V], *args, **kwargs) -> V:
    qtbot.waitUntil(lambda: gui.findChild(typ, *args, **kwargs) is not None)
    return get_child(gui, typ, *args, **kwargs)


def get_child(gui: QWidget, typ: Type[V], *args, **kwargs) -> V:
    child = gui.findChild(typ, *args, **kwargs)
    assert isinstance(child, typ)
    return child


def get_children(gui: QWidget, typ: Type[V], *args, **kwargs) -> List[V]:
    children: List[typ] = gui.findChildren(typ, *args, **kwargs)
    return children
