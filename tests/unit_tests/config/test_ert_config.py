import fileinput
import json
import logging
import os
import os.path
import stat
from datetime import date
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock, mock_open, patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ert.config import AnalysisConfig, ConfigValidationError, ErtConfig, HookRuntime
from ert.config.ert_config import site_config_location
from ert.config.parsing import ConfigKeys, ConfigWarning
from ert.config.parsing.context_values import (
    ContextBool,
    ContextBoolEncoder,
    ContextFloat,
    ContextInt,
    ContextList,
    ContextString,
)
from ert.config.parsing.observations_parser import ObservationConfigError

from .config_dict_generator import config_generators

config_defines = {
    "<USER>": "TEST_USER",
    "<SCRATCH>": "ert/model/scratch/ert",
    "<CASE_DIR>": "the_extensive_case",
    "<ECLIPSE_NAME>": "XYZ",
}

snake_oil_structure_config = {
    "RUNPATH": "<SCRATCH>/<USER>/<CASE_DIR>/realization-<IENS>/iter-<ITER>",
    "NUM_REALIZATIONS": 10,
    "MAX_RUNTIME": 23400,
    "MIN_REALIZATIONS": "50%",
    "MAX_SUBMIT": 13,
    "QUEUE_SYSTEM": "LSF",
    "LSF_QUEUE": "mr",
    "LSF_SERVER": "simulacrum",
    "LSF_RESOURCE": "select[x86_64Linux] same[type:model]",
    "MAX_RUNNING": "100",
    "DATA_FILE": "eclipse/model/SNAKE_OIL.DATA",
    "START": date(2017, 1, 1),
    "SUMMARY": [
        "WOPR:PROD",
        "WOPT:PROD",
        "WWPR:PROD",
        "WWCT:PROD",
        "WWPT:PROD",
        "WBHP:PROD",
        "WWIR:INJ",
        "WWIT:INJ",
        "WBHP:INJ",
        "ROE:1",
    ],
    "GEN_KW": ["SIGMA"],
    "GEN_DATA": ["super_data"],
    "ECLBASE": "eclipse/model/<ECLIPSE_NAME>-<IENS>",
    "ENSPATH": "ert/output/storage/<CASE_DIR>",
    "UPDATE_LOG_PATH": "../output/update_log/<CASE_DIR>",
    "RUNPATH_FILE": "ert/output/run_path_file/.ert-runpath-list_<CASE_DIR>",
    "REFCASE": "ert/input/refcase/SNAKE_OIL_FIELD",
    "SIGMA": {
        "TEMPLATE": "ert/input/templates/sigma.tmpl",
        "RESULT": "coarse.sigma",
        "PARAMETER": "ert/input/distributions/sigma.dist",
    },
    "JOBNAME": "SNAKE_OIL_STRUCTURE_<IENS>",
    "INSTALL_JOB": {
        "SNAKE_OIL_SIMULATOR": {
            "CONFIG": "snake_oil/jobs/SNAKE_OIL_SIMULATOR",
            "STDOUT": "snake_oil.stdout",
            "STDERR": "snake_oil.stderr",
            "EXECUTABLE": "snake_oil_simulator.py",
        },
        "SNAKE_OIL_NPV": {
            "CONFIG": "snake_oil/jobs/SNAKE_OIL_NPV",
            "STDOUT": "snake_oil_npv.stdout",
            "STDERR": "snake_oil_npv.stderr",
            "EXECUTABLE": "snake_oil_npv.py",
        },
        "SNAKE_OIL_DIFF": {
            "CONFIG": "snake_oil/jobs/SNAKE_OIL_DIFF",
            "STDOUT": "snake_oil_diff.stdout",
            "STDERR": "snake_oil_diff.stderr",
            "EXECUTABLE": "snake_oil_diff.py",
        },
    },
    "FORWARD_MODEL": ["SNAKE_OIL_SIMULATOR", "SNAKE_OIL_NPV", "SNAKE_OIL_DIFF"],
    "HISTORY_SOURCE": "REFCASE_HISTORY",
    "OBS_CONFIG": "ert/input/observations/obsfiles/observations.txt",
    "LOAD_WORKFLOW": [["ert/bin/workflows/MAGIC_PRINT", "MAGIC_PRINT"]],
    "LOAD_WORKFLOW_JOB": [
        ["ert/bin/workflows/workflowjobs/bin/uber_print.py", "UBER_PRINT"],
    ],
    "GRID": "eclipse/include/grid/CASE.EGRID",
    "RUN_TEMPLATE": {
        "seed_template": {
            "TEMPLATE_FILE": "ert/input/templates/seed_template.txt",
            "TARGET_FILE": "seed.txt",
        }
    },
}


def expand_config_defs(defines, config):
    for define_key, define_value in defines.items():
        for data_key, data_value in config.items():
            if isinstance(data_value, str):
                config[data_key] = data_value.replace(define_key, define_value)


# Expand all strings in snake oil structure config according to defines.
expand_config_defs(config_defines, snake_oil_structure_config)


def test_include_existing_file(tmpdir):
    with tmpdir.as_cwd():
        config = """
        JOBNAME my_name%d
        INCLUDE include_me
        NUM_REALIZATIONS 1
        """
        rand_seed = 420
        include_me_text = f"""
        RANDOM_SEED {rand_seed}
        """

        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.writelines(config)

        with open("include_me", mode="w", encoding="utf-8") as fh:
            fh.writelines(include_me_text)

        ert_config = ErtConfig.from_file("config.ert")
        assert ert_config.random_seed == rand_seed


def test_init(minimum_case):
    ert_config = minimum_case

    assert ert_config is not None

    assert ert_config.analysis_config is not None
    assert isinstance(ert_config.analysis_config, AnalysisConfig)

    assert ert_config.config_path == os.getcwd()

    assert ert_config.substitution_list["<CONFIG_PATH>"] == os.getcwd()


def test_runpath_file(monkeypatch, tmp_path):
    """
    There was an issue relating to `ErtConfig.runpath_file` returning a
    relative path rather than an absolute path. This test simulates the
    conditions that caused the original bug. That is, the user starts
    somewhere else and points to the ERT config file using a relative
    path.
    """
    config_path = tmp_path / "model/ert/config.ert"
    workdir_path = tmp_path / "start/from/here"
    runpath_path = tmp_path / "model/output/my_custom_runpath_path.foo"

    config_path.parent.mkdir(parents=True)
    workdir_path.mkdir(parents=True)
    monkeypatch.chdir(workdir_path)

    with config_path.open("w") as f:
        f.writelines(
            [
                "DEFINE <FOO> foo\n",
                "RUNPATH_FILE ../output/my_custom_runpath_path.<FOO>\n",
                # Required for this to be a valid ErtConfig
                "NUM_REALIZATIONS 1\n",
            ]
        )

    config = ErtConfig.from_file(os.path.relpath(config_path, workdir_path))
    assert config.runpath_file == runpath_path


def test_that_job_script_can_be_set_in_site_config(monkeypatch, tmp_path):
    """
    We use the jobscript field to inject a komodo environment onprem.
    This overwrites the value by appending to the default siteconfig.
    Need to check that the second JOB_SCRIPT is the one that gets used.
    """
    test_site_config = tmp_path / "test_site_config.ert"
    my_script = (tmp_path / "my_script").resolve()
    my_script.write_text("")
    st = os.stat(my_script)
    os.chmod(my_script, st.st_mode | stat.S_IEXEC)
    test_site_config.write_text(
        f"JOB_SCRIPT job_dispatch.py\nJOB_SCRIPT {my_script}\nQUEUE_SYSTEM LOCAL\n"
    )
    monkeypatch.setenv("ERT_SITE_CONFIG", str(test_site_config))

    test_user_config = tmp_path / "user_config.ert"

    test_user_config.write_text(
        "JOBNAME  Job%d\nRUNPATH /tmp/simulations/realization-<IENS>/iter-<ITER>\n"
        "NUM_REALIZATIONS 10\n"
    )

    ert_config = ErtConfig.from_file(str(test_user_config))

    assert Path(ert_config.queue_config.job_script).resolve() == my_script


@pytest.mark.parametrize(
    "run_mode",
    [
        HookRuntime.POST_SIMULATION,
        HookRuntime.PRE_SIMULATION,
        HookRuntime.PRE_FIRST_UPDATE,
        HookRuntime.PRE_UPDATE,
        HookRuntime.POST_UPDATE,
    ],
)
def test_that_workflow_run_modes_can_be_selected(tmp_path, run_mode):
    my_script = (tmp_path / "my_script").resolve()
    my_script.write_text("")
    st = os.stat(my_script)
    os.chmod(my_script, st.st_mode | stat.S_IEXEC)
    test_user_config = tmp_path / "user_config.ert"
    test_user_config.write_text(
        "JOBNAME  Job%d\nRUNPATH /tmp/simulations/realization-<IENS>/iter-<ITER>\n"
        "NUM_REALIZATIONS 10\n"
        f"LOAD_WORKFLOW {my_script} SCRIPT\n"
        f"HOOK_WORKFLOW SCRIPT {run_mode.name}\n"
    )
    ert_config = ErtConfig.from_file(str(test_user_config))
    assert len(list(ert_config.hooked_workflows[run_mode])) == 1


@pytest.mark.parametrize(
    "config_content, expected",
    [
        pytest.param("--Comment", "", id="Line comment"),
        pytest.param(" --Comment", "", id="Line comment with whitespace"),
        pytest.param("\t--Comment", "", id="Line comment with whitespace"),
        pytest.param("KEY VALUE", "KEY VALUE\n", id="Config line"),
        pytest.param("KEY VALUE --Comment", "KEY VALUE\n", id="Inline comment"),
    ],
)
def test_logging_config(caplog, config_content, expected):
    base_content = "Content of the configuration file (file_name):\n{}"
    config_path = "file_name"

    with patch("builtins.open", mock_open(read_data=config_content)), patch(
        "os.path.isfile", MagicMock(return_value=True)
    ), caplog.at_level(logging.INFO):
        ErtConfig._log_config_file(config_path)
    expected = base_content.format(expected)
    assert expected in caplog.messages


@pytest.mark.usefixtures("use_tmpdir")
def test_logging_with_comments(caplog):
    """
    Run logging on an actual config file with line comments
    and inline comments to check the result
    """

    config = dedent(
        """
    NUM_REALIZATIONS 1 -- inline comment
    -- Regular comment
    ECLBASE PRED_RUN
    SUMMARY *
    """
    )
    with open("config.ert", "w", encoding="utf-8") as fh:
        fh.writelines(config)
    with caplog.at_level(logging.INFO):
        ErtConfig._log_config_file("config.ert")
    assert (
        """
NUM_REALIZATIONS 1
ECLBASE PRED_RUN
SUMMARY *"""
        in caplog.text
    )


@pytest.mark.usefixtures("use_tmpdir")
def test_that_parsing_workflows_gives_expected():
    ERT_SITE_CONFIG = site_config_location()
    ERT_SHARE_PATH = os.path.dirname(ERT_SITE_CONFIG)
    cwd = os.getcwd()

    config_dict = {
        ConfigKeys.LOAD_WORKFLOW_JOB: [
            [cwd + "/workflows/UBER_PRINT", "print_uber"],
            [cwd + "/workflows/HIDDEN_PRINT", "HIDDEN_PRINT"],
        ],
        ConfigKeys.LOAD_WORKFLOW: [
            [cwd + "/workflows/MAGIC_PRINT", "magic_print"],
            [cwd + "/workflows/NO_PRINT", "no_print"],
            [cwd + "/workflows/SOME_PRINT", "some_print"],
        ],
        ConfigKeys.WORKFLOW_JOB_DIRECTORY: [
            ERT_SHARE_PATH + "/workflows/jobs/shell",
            ERT_SHARE_PATH + "/workflows/jobs/internal-gui/config",
        ],
        ConfigKeys.HOOK_WORKFLOW: [
            ["magic_print", "POST_UPDATE"],
            ["no_print", "PRE_UPDATE"],
        ],
        ConfigKeys.ENSPATH: "enspath",
        ConfigKeys.NUM_REALIZATIONS: 1,
    }

    with open("minimum_config", "a+", encoding="utf-8") as ert_file:
        ert_file.write("LOAD_WORKFLOW_JOB workflows/UBER_PRINT print_uber\n")
        ert_file.write("LOAD_WORKFLOW_JOB workflows/HIDDEN_PRINT\n")
        ert_file.write("LOAD_WORKFLOW workflows/MAGIC_PRINT magic_print\n")
        ert_file.write("LOAD_WORKFLOW workflows/NO_PRINT no_print\n")
        ert_file.write("LOAD_WORKFLOW workflows/SOME_PRINT some_print\n")
        ert_file.write("HOOK_WORKFLOW magic_print POST_UPDATE\n")
        ert_file.write("HOOK_WORKFLOW no_print PRE_UPDATE\n")

    os.mkdir("workflows")

    with open("workflows/MAGIC_PRINT", "w", encoding="utf-8") as f:
        f.write("print_uber\n")
    with open("workflows/NO_PRINT", "w", encoding="utf-8") as f:
        f.write("print_uber\n")
    with open("workflows/SOME_PRINT", "w", encoding="utf-8") as f:
        f.write("print_uber\n")
    with open("workflows/UBER_PRINT", "w", encoding="utf-8") as f:
        f.write("EXECUTABLE ls\n")
    with open("workflows/HIDDEN_PRINT", "w", encoding="utf-8") as f:
        f.write("EXECUTABLE ls\n")

    ert_config = ErtConfig.from_dict(config_dict)

    # verify name generated from filename
    assert "HIDDEN_PRINT" in ert_config.workflow_jobs
    assert "print_uber" in ert_config.workflow_jobs

    assert [
        "magic_print",
        "no_print",
        "some_print",
    ] == list(ert_config.workflows.keys())

    assert len(ert_config.hooked_workflows[HookRuntime.PRE_UPDATE]) == 1
    assert len(ert_config.hooked_workflows[HookRuntime.POST_UPDATE]) == 1
    assert len(ert_config.hooked_workflows[HookRuntime.PRE_FIRST_UPDATE]) == 0


@pytest.mark.usefixtures("use_tmpdir")
def test_that_get_plugin_jobs_fetches_exactly_ert_plugins():
    script_file_contents = dedent(
        """
        SCRIPT script.py
        INTERNAL True
        """
    )
    plugin_file_contents = dedent(
        """
        SCRIPT plugin.py
        INTERNAL True
        """
    )

    script_file_path = os.path.join(os.getcwd(), "script")
    plugin_file_path = os.path.join(os.getcwd(), "plugin")
    with open(script_file_path, mode="w", encoding="utf-8") as fh:
        fh.write(script_file_contents)
    with open(plugin_file_path, mode="w", encoding="utf-8") as fh:
        fh.write(plugin_file_contents)

    with open("script.py", mode="w", encoding="utf-8") as fh:
        fh.write(
            dedent(
                """
                from ert import ErtScript
                class Script(ErtScript):
                    def run(self, *args):
                        pass
                """
            )
        )
    with open("plugin.py", mode="w", encoding="utf-8") as fh:
        fh.write(
            dedent(
                """
                from ert.config import ErtPlugin
                class Plugin(ErtPlugin):
                    def run(self, *args):
                        pass
                """
            )
        )
    with open("config.ert", mode="w", encoding="utf-8") as fh:
        fh.write(
            dedent(
                f"""
                NUM_REALIZATIONS 1
                LOAD_WORKFLOW_JOB {plugin_file_path} plugin
                LOAD_WORKFLOW_JOB {script_file_path} script
                """
            )
        )

    ert_config = ErtConfig.from_file("config.ert")

    assert ert_config.workflow_jobs["plugin"].is_plugin()
    assert not ert_config.workflow_jobs["script"].is_plugin()


def test_data_file_with_non_utf_8_character_gives_error_message(tmpdir):
    with tmpdir.as_cwd():
        data_file = "data_file.DATA"
        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.write(
                """NUM_REALIZATIONS 1
            DATA_FILE data_file.DATA
            ECLBASE data_file_<ITER>
            """
            )
        with open(data_file, mode="w", encoding="utf-8") as fh:
            fh.write(
                dedent(
                    """
                        START
                        --  DAY   MONTH  YEAR
                        1    'JAN'  2017   /
                    """
                )
            )
        with open(data_file, "ab") as f:
            f.write(b"\xff")
        data_file_path = f"{tmpdir}/{data_file}"
        with pytest.raises(
            ConfigValidationError,
            match="Unsupported non UTF-8 character "
            f"'ÿ' found in file: {data_file_path!r}",
        ):
            ErtConfig.from_file("config.ert")


def test_that_double_comments_are_handled(tmpdir):
    with tmpdir.as_cwd():
        with open("config.ert", mode="w", encoding="utf-8") as fh:
            fh.write(
                """NUM_REALIZATIONS 1 -- foo -- bar -- 2
                   JOBNAME &SUM$VAR@12@#£¤<
            """
            )
        ert_config = ErtConfig.from_file("config.ert")
        assert ert_config.model_config.num_realizations == 1
        assert ert_config.model_config.jobname_format_string == "&SUM$VAR@12@#£¤<"


@pytest.mark.filterwarnings("ignore:.*Unknown keyword.*:ert.config.ConfigWarning")
def test_bad_user_config_file_error_message(tmp_path):
    (tmp_path / "test.ert").write_text("NUM_REL 10\n")
    with pytest.raises(ConfigValidationError, match="NUM_REALIZATIONS must be set"):
        _ = ErtConfig.from_file(str(tmp_path / "test.ert"))


@pytest.mark.usefixtures("use_tmpdir")
def test_ert_config_parses_date():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <STORAGE> storage/<CONFIG_FILE_BASE>-<DATE>
        RUNPATH <STORAGE>/runpath/realization-<IENS>/iter-<ITER>
        ENSPATH <STORAGE>/ensemble
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    ert_config = ErtConfig.from_file(test_config_file_name)

    date_string = date.today().isoformat()
    expected_storage = os.path.abspath(f"storage/{test_config_file_base}-{date_string}")
    expected_run_path = f"{expected_storage}/runpath/realization-<IENS>/iter-<ITER>"
    expected_ens_path = f"{expected_storage}/ensemble"
    assert ert_config.ens_path == expected_ens_path
    assert ert_config.model_config.runpath_format_string == expected_run_path


@pytest.mark.usefixtures("use_tmpdir")
def test_that_subst_list_is_given_default_runpath_file():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = "NUM_REALIZATIONS 1"
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    ert_config = ErtConfig.from_file(test_config_file_name)
    assert ert_config.substitution_list["<RUNPATH_FILE>"] == os.path.abspath(
        ErtConfig.DEFAULT_RUNPATH_FILE
    )


@pytest.mark.usefixtures("set_site_config")
@settings(max_examples=10)
@given(config_generators())
def test_that_creating_ert_config_from_dict_is_same_as_from_file(
    tmp_path_factory, config_generator
):
    filename = "config.ert"
    with config_generator(tmp_path_factory, filename) as config_values:
        assert ErtConfig.from_dict(
            config_values.to_config_dict("config.ert", os.getcwd())
        ) == ErtConfig.from_file(filename)


@pytest.mark.usefixtures("set_site_config")
@settings(max_examples=10)
@given(config_generators())
def test_that_parsing_ert_config_result_in_expected_values(
    tmp_path_factory, config_generator
):
    filename = "config.ert"
    with config_generator(tmp_path_factory, filename) as config_values:
        ert_config = ErtConfig.from_file(filename)
        assert ert_config.ens_path == config_values.enspath
        assert ert_config.random_seed == config_values.random_seed
        assert ert_config.queue_config.max_submit == config_values.max_submit
        assert ert_config.queue_config.job_script == config_values.job_script
        assert ert_config.user_config_file == os.path.abspath(filename)
        assert ert_config.config_path == os.getcwd()
        assert str(ert_config.runpath_file) == os.path.abspath(
            config_values.runpath_file
        )
        assert (
            ert_config.model_config.num_realizations == config_values.num_realizations
        )


def test_default_ens_path(tmpdir):
    with tmpdir.as_cwd():
        config_file = "test.ert"
        with open(config_file, "w", encoding="utf-8") as f:
            f.write(
                """
NUM_REALIZATIONS  1
            """
            )
        ert_config = ErtConfig.from_file(config_file)
        # By default, the ensemble path is set to 'storage'
        default_ens_path = ert_config.ens_path

        with open(config_file, "a", encoding="utf-8") as f:
            f.write(
                """
ENSPATH storage
            """
            )

        # Set the ENSPATH in the config file
        ert_config = ErtConfig.from_file(config_file)
        set_in_file_ens_path = ert_config.ens_path

        assert default_ens_path == set_in_file_ens_path

        config_dict = {
            ConfigKeys.NUM_REALIZATIONS: 1,
            "ENSPATH": os.path.join(os.getcwd(), "storage"),
        }

        dict_set_ens_path = ErtConfig.from_dict(config_dict).ens_path

        assert dict_set_ens_path == config_dict["ENSPATH"]


@pytest.mark.usefixtures("use_tmpdir")
@pytest.mark.parametrize(
    "max_running_value, expected_error",
    [
        (100, None),  # positive integer
        (-1, "is not a valid positive integer"),  # negative integer
        ("not_an_integer", "is not a valid positive integer"),  # non-integer
    ],
)
def test_queue_config_max_running_invalid_values(max_running_value, expected_error):
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        f"""
        NUM_REALIZATIONS  1
        DEFINE <STORAGE> storage/<CONFIG_FILE_BASE>-<DATE>
        RUNPATH <STORAGE>/runpath/realization-<IENS>/iter-<ITER>
        ENSPATH <STORAGE>/ensemble
        QUEUE_SYSTEM LOCAL
        QUEUE_OPTION LOCAL MAX_RUNNING {max_running_value}
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    if expected_error:
        with pytest.raises(
            expected_exception=ConfigValidationError,
            match=expected_error,
        ):
            ErtConfig.from_file(test_config_file_name)
    else:
        ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
@given(st.integers(min_value=0), st.integers(min_value=0), st.integers(min_value=0))
def test_num_cpu_vs_torque_queue_cpu_configuration(
    num_cpu_int, num_nodes_int, num_cpus_per_node_int
):

    # Only strictly positive ints are valid configuration values,
    # zero values are used to represent the "not configured" scenario:
    num_cpu = str(num_cpu_int) if num_cpu_int > 0 else ""
    num_nodes = str(num_nodes_int) if num_nodes_int > 0 else ""
    num_cpus_per_node = str(num_cpus_per_node_int) if num_cpus_per_node_int > 0 else ""

    expected_error = (
        "product .* must be equal"
        if (num_cpu_int or 1) != (num_nodes_int or 1) * (num_cpus_per_node_int or 1)
        else None
    )

    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS 1
        DEFINE <STORAGE> storage/<CONFIG_FILE_BASE>-<DATE>
        RUNPATH <STORAGE>/runpath/realization-<IENS>/iter-<ITER>
        ENSPATH <STORAGE>/ensemble
        QUEUE_SYSTEM TORQUE
        """
    )
    if num_cpu:
        test_config_contents += f"NUM_CPU {num_cpu}\n"
    if num_nodes:
        test_config_contents += f"QUEUE_OPTION TORQUE NUM_NODES {num_nodes}\n"
    if num_cpus_per_node:
        test_config_contents += (
            f"QUEUE_OPTION TORQUE NUM_CPUS_PER_NODE {num_cpus_per_node}\n"
        )

    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    if expected_error:
        with pytest.raises(
            expected_exception=ConfigValidationError,
            match=expected_error,
        ):
            ErtConfig.from_file(test_config_file_name)
    else:
        ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_non_existant_job_directory_gives_config_validation_error():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <STORAGE> storage/<CONFIG_FILE_BASE>-<DATE>
        RUNPATH <STORAGE>/runpath/realization-<IENS>/iter-<ITER>
        ENSPATH <STORAGE>/ensemble
        INSTALL_JOB_DIRECTORY does_not_exist
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with pytest.raises(
        expected_exception=ConfigValidationError,
        match="Unable to locate job directory",
    ):
        ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_empty_job_directory_gives_warning():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <STORAGE> storage/<CONFIG_FILE_BASE>-<DATE>
        RUNPATH <STORAGE>/runpath/realization-<IENS>/iter-<ITER>
        ENSPATH <STORAGE>/ensemble
        INSTALL_JOB_DIRECTORY empty
        """
    )
    os.mkdir("empty")
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with pytest.warns(ConfigWarning, match="No files found in job directory"):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_recursive_define_warns_when_early_liveness_detection_mechanism_triggers():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <A> <A>
        RUNPATH runpath/realization-<IENS>/iter-<ITER>/<A>
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with pytest.warns(
        ConfigWarning,
        match="Gave up replacing in runpath/realization-<IENS>/iter-<ITER>/<A>."
        "\nAfter replacing the value is now: "
        "runpath/realization-<IENS>/iter-<ITER>/<A>.\n"
        "This still contains the replacement value: <A>, "
        "which would be replaced by <A>. Probably this causes a loop.",
    ):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_recursive_define_warns_when_reached_max_iterations():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <A> a/<A>
        RUNPATH runpath/realization-<IENS>/iter-<ITER>/<A>
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with pytest.warns(
        ConfigWarning,
        match="Gave up replacing in runpath/realization-<IENS>/iter-<ITER>/<A>.\n"
        "After replacing the value is now: "
        "runpath/realization-<IENS>/iter-<ITER>/(a/){100}<A>.\n"
        "This still contains the replacement value: <A>, "
        "which would be replaced by a/<A>. Probably this causes a loop.",
    ):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_loading_non_existant_workflow_gives_validation_error():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        LOAD_WORKFLOW does_not_exist
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with pytest.raises(
        expected_exception=ConfigValidationError,
        match='Cannot find file or directory "does_not_exist"',
    ):
        ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_loading_non_existant_workflow_job_gives_validation_error():
    test_config_file_base = "test"
    test_config_file_name = f"{test_config_file_base}.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        LOAD_WORKFLOW_JOB does_not_exist
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with pytest.raises(
        expected_exception=ConfigValidationError,
        match='Cannot find file or directory "does_not_exist"',
    ):
        ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_job_definition_file_with_unexecutable_script_gives_validation_error():
    test_config_file_name = "test.ert"
    job_script_file = os.path.abspath("not_executable")
    job_name = "JOB_NAME"
    test_config_contents = dedent(
        f"""
        NUM_REALIZATIONS  1
        LOAD_WORKFLOW_JOB {job_name}
        """
    )
    with open(job_name, "w", encoding="utf-8") as fh:
        fh.write(f"EXECUTABLE {job_script_file}\n")
    with open(job_script_file, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\n")
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with pytest.raises(
        expected_exception=ConfigValidationError,
    ):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.parametrize("c", ["\\", "?", "+", ":", "*"])
@pytest.mark.usefixtures("use_tmpdir")
def test_char_in_unquoted_is_allowed(c):
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        f"""
        NUM_REALIZATIONS 1
        RUNPATH path{c}a/b
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert f"path{c}a/b" in ert_config.model_config.runpath_format_string


@pytest.mark.usefixtures("use_tmpdir")
def test_that_magic_strings_get_substituted_in_workflow():
    script_file_contents = dedent(
        """
        SCRIPT script.py
        ARGLIST <A>
        ARG_TYPE 0 INT
        """
    )
    workflow_file_contents = dedent(
        """
        script <ZERO>
        """
    )
    script_file_path = os.path.join(os.getcwd(), "script")
    workflow_file_path = os.path.join(os.getcwd(), "workflow")
    with open(script_file_path, mode="w", encoding="utf-8") as fh:
        fh.write(script_file_contents)
    with open(workflow_file_path, mode="w", encoding="utf-8") as fh:
        fh.write(workflow_file_contents)

    with open("script.py", mode="w", encoding="utf-8") as fh:
        fh.write(
            dedent(
                """
                from ert import ErtScript
                class Script(ErtScript):
                    def run(self, *args):
                        pass
                """
            )
        )
    with open("config.ert", mode="w", encoding="utf-8") as fh:
        fh.write(
            dedent(
                f"""
                NUM_REALIZATIONS 1
                DEFINE <ZERO> 0
                LOAD_WORKFLOW_JOB {script_file_path} script
                LOAD_WORKFLOW {workflow_file_path}
                """
            )
        )

    ert_config = ErtConfig.from_file("config.ert")

    assert ert_config.workflows["workflow"].cmd_list[0][1] == ["0"]


@pytest.mark.usefixtures("use_tmpdir")
def test_that_unknown_job_gives_config_validation_error():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        SIMULATION_JOB NO_SUCH_JOB
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.raises(ConfigValidationError, match="Could not find job 'NO_SUCH_JOB'"):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_unknown_hooked_job_gives_config_validation_error():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        HOOK_WORKFLOW NO_SUCH_JOB PRE_SIMULATION
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.raises(
        ConfigValidationError,
        match="Cannot setup hook for non-existing job name 'NO_SUCH_JOB'",
    ):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("set_site_config")
@settings(max_examples=10)
@given(config_generators())
def test_that_if_field_is_given_and_grid_is_missing_you_get_error(
    tmp_path_factory, config_generator
):
    with config_generator(tmp_path_factory) as config_values:
        config_dict = config_values.to_config_dict("test.ert", os.getcwd())
        del config_dict[ConfigKeys.GRID]
        assume(len(config_dict.get(ConfigKeys.FIELD, [])) > 0)
        with pytest.raises(
            ConfigValidationError,
            match="In order to use the FIELD keyword, a GRID must be supplied",
        ):
            _ = ErtConfig.from_dict(config_dict)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_include_statements_with_multiple_values_raises_error():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        INCLUDE this and that and some-other
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.raises(
        ConfigValidationError, match="Keyword:INCLUDE must have exactly one argument"
    ):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_workflows_with_errors_are_not_loaded():
    """
    The user may install several workflows with LOAD_WORKFLOW_DIRECTORY that
    does not work with the current versions of plugins installed in the system,
    but could have worked with an older or newer version of the packages installed.

    Therefore the user should be warned about workflows that have issues, and not be
    able to run those later. If a workflow with errors are hooked, then the user will
    get an error indicating that there is no such workflow.
    """
    test_config_file_name = "test.ert"
    Path("WFJOB").write_text("EXECUTABLE echo\n", encoding="utf-8")
    # intentionally misspelled WFJOB as WFJAB
    Path("wf").write_text("WFJAB hello world\n", encoding="utf-8")
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        JOBNAME JOOOOOB
        LOAD_WORKFLOW_JOB WFJOB
        LOAD_WORKFLOW wf
        """
    )

    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.warns(
        ConfigWarning,
        match=r"Encountered the following error\(s\) while reading workflow 'wf'."
        " It will not be loaded: .*WFJAB is not recognized",
    ):
        ert_config = ErtConfig.from_file(test_config_file_name)
        assert "wf" not in ert_config.workflows


@pytest.mark.usefixtures("use_tmpdir")
def test_that_workflow_job_subdirectories_are_not_loaded():
    test_config_file = Path("test.ert")
    wfjob_dir = Path("WFJOBS")
    wfjob_dir.mkdir()
    (wfjob_dir / "wfjob").write_text("EXECUTABLE echo\n", encoding="utf-8")
    (wfjob_dir / "subdir").mkdir()
    test_config_file.write_text(
        dedent(
            f"""
        NUM_REALIZATIONS  1
        JOBNAME JOOOOOB
        WORKFLOW_JOB_DIRECTORY {wfjob_dir}
        """
        ),
        encoding="utf-8",
    )

    ert_config = ErtConfig.from_file(str(test_config_file))
    assert "wfjob" in ert_config.workflow_jobs


@pytest.mark.usefixtures("use_tmpdir")
def test_that_adding_a_workflow_twice_warns():
    test_config_file_name = "test.ert"
    Path("WFJOB").write_text("EXECUTABLE echo\n", encoding="utf-8")
    Path("wf").write_text("WFJOB hello world\n", encoding="utf-8")
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        JOBNAME JOOOOOB
        LOAD_WORKFLOW_JOB WFJOB
        LOAD_WORKFLOW wf
        LOAD_WORKFLOW wf
        """
    )

    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.warns(
        ConfigWarning,
        match=r"Workflow 'wf' was added twice",
    ) as warnlog:
        _ = ErtConfig.from_file(test_config_file_name)

    assert any("test.ert: Line 6" in str(w.message) for w in warnlog)


@pytest.mark.usefixtures("use_tmpdir")
@pytest.mark.parametrize(
    "load_statement", ["LOAD_WORKFLOW_JOB wfs/WFJOB", "WORKFLOW_JOB_DIRECTORY wfs"]
)
def test_that_failing_to_load_ert_script_with_errors_fails_gracefully(load_statement):
    """
    The user may install several workflow jobs with LOAD_WORKFLOW_JOB_DIRECTORY that
    does not work with the current versions of plugins installed in the system,
    but could have worked with an older or newer version of the packages installed.

    Therefore the user should be warned about workflow jobs that have issues, and not be
    able to run those later.
    """
    test_config_file_name = "test.ert"
    Path("wfs").mkdir()
    Path("wfs/WFJOB").write_text("SCRIPT wf_script.py\nINTERNAL True", encoding="utf-8")
    Path("wf_script.py").write_text("", encoding="utf-8")
    test_config_contents = dedent(
        f"""
        NUM_REALIZATIONS  1
        {load_statement}
        """
    )

    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.warns(
        ConfigWarning, match="Loading workflow job.*failed.*It will not be loaded."
    ):
        ert_config = ErtConfig.from_file(test_config_file_name)
        assert "wf" not in ert_config.workflows


@pytest.mark.usefixtures("use_tmpdir")
def test_that_define_statements_with_less_than_one_argument_raises_error():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <USER>
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.raises(
        ConfigValidationError,
        match="DEFINE must have (two or more|at least 2) arguments",
    ):
        _ = ErtConfig.from_file(test_config_file_name)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_define_statements_with_more_than_one_argument():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <TEST1> 111 222 333
        DEFINE <TEST2> <TEST1> 444 555
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert ert_config.substitution_list.get("<TEST1>") == "111 222 333"
    assert ert_config.substitution_list.get("<TEST2>") == "111 222 333 444 555"


@pytest.mark.usefixtures("use_tmpdir")
def test_that_include_statements_work():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        INCLUDE include.ert
        """
    )
    test_include_file_name = "include.ert"
    test_include_contents = dedent(
        """
        JOBNAME included
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with open(test_include_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_include_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert ert_config.model_config.jobname_format_string == "included"


@pytest.mark.usefixtures("use_tmpdir")
def test_that_define_string_quotes_are_removed():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        DEFINE <A> "A"
        NUM_REALIZATIONS 1
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    ert_Config = ErtConfig.from_file(test_config_file_name)
    assert ert_Config.substitution_list.get("<A>") == "A"


@pytest.mark.usefixtures("use_tmpdir")
def test_that_included_files_uses_paths_relative_to_itself():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS 1
        INCLUDE includes/install_jobs.ert
        """
    )
    os.mkdir("includes")
    test_include_file_name = "includes/install_jobs.ert"
    test_include_contents = dedent(
        """
        INSTALL_JOB FM ../FM
        """
    )
    test_fm_file_name = "FM"
    test_fm_contents = dedent(
        """
        EXECUTABLE echo
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with open(test_include_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_include_contents)
    with open(test_fm_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_fm_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert ert_config.installed_jobs["FM"].name == "FM"


@pytest.mark.usefixtures("use_tmpdir")
@pytest.mark.parametrize("val, expected", [("TrUe", True), ("FaLsE", False)])
def test_that_boolean_values_can_be_any_case(val, expected):
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        f"""
        NUM_REALIZATIONS  1
        STOP_LONG_RUNNING {val}
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert ert_config.analysis_config.stop_long_running == expected


@pytest.mark.usefixtures("use_tmpdir", "set_site_config")
def test_that_include_take_into_account_path():
    """
    Tests that use_new_parser resolves an issue
    with the old parser where the first relative path
    FORWARD_MODEL is chosen for all.
    """
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        INCLUDE dir/include.ert
        INSTALL_JOB job2 job2
        """
    )
    test_include_file_name = "dir/include.ert"
    test_include_contents = dedent(
        """
        INSTALL_JOB job1 job1
        """
    )
    # The old parser tries to find dir/job2
    os.mkdir("dir")
    Path("dir/job1").write_text("EXECUTABLE echo\n", encoding="utf-8")
    Path("job2").write_text("EXECUTABLE ls\n", encoding="utf-8")
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with open(test_include_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_include_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert list(ert_config.installed_jobs.keys()) == [
        "job1",
        "job2",
    ]


@pytest.mark.usefixtures("use_tmpdir", "set_site_config")
def test_that_substitution_happens_for_include():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        DEFINE <file> include.ert
        INCLUDE dir/<file>
        """
    )
    test_include_file_name = "dir/include.ert"
    test_include_contents = dedent(
        """
        RUNPATH my_silly_runpath<ITER>-<IENS>
        """
    )
    # The old parser tries to find dir/job2
    os.mkdir("dir")
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with open(test_include_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_include_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert (
        "my_silly_runpath<ITER>-<IENS>" in ert_config.model_config.runpath_format_string
    )


@pytest.mark.usefixtures("use_tmpdir", "set_site_config")
def test_that_defines_in_included_files_has_immediate_effect():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        INCLUDE dir/include.ert
        RUNPATH <FOO>-<ITER>-<IENS>
        DEFINE <FOO> bar
        """
    )
    test_include_file_name = "dir/include.ert"
    test_include_contents = dedent(
        """
        DEFINE <FOO> baz
        """
    )
    # The old parser tries to find dir/job2
    os.mkdir("dir")
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
    with open(test_include_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_include_contents)

    ert_config = ErtConfig.from_file(test_config_file_name)
    assert "baz-<ITER>-<IENS>" in ert_config.model_config.runpath_format_string


@pytest.mark.usefixtures("use_tmpdir", "set_site_config")
def test_that_multiple_errors_are_shown_for_forward_model():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        FORWARD_MODEL does_not_exist
        FORWARD_MODEL does_not_exist2
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    with pytest.raises(ConfigValidationError) as err:
        _ = ErtConfig.from_file(test_config_file_name)

    expected_nice_messages_list = [
        (
            "test.ert: Line 3 (Column 15-29): "
            "Could not find job 'does_not_exist' "
            "in list of installed jobs: []"
        ),
        (
            "test.ert: Line 4 (Column 15-30): "
            "Could not find job 'does_not_exist2' "
            "in list of installed jobs: []"
        ),
    ]

    cli_message = err.value.cli_message()

    for msg in expected_nice_messages_list:
        assert any(line.endswith(msg) for line in cli_message.splitlines())


@pytest.mark.usefixtures("use_tmpdir")
def test_that_redefines_work_with_setenv():
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS 1
        DEFINE <X> 3
        SETENV VAR <X>
        DEFINE <X> 4
        SETENV VAR2 <X>
    """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)

    ert_config = ErtConfig.from_file(user_config_file=test_config_file_name)

    assert ert_config.env_vars["VAR"] == "3"
    assert ert_config.env_vars["VAR2"] == "4"


@pytest.mark.usefixtures("use_tmpdir")
def test_parsing_workflow_with_multiple_args():
    script_file_contents = dedent(
        """
        SCRIPT script.py
        ARGLIST <A> -r <B> -t <C>
        """
    )
    workflow_file_contents = dedent(
        """
        script <ZERO>
        """
    )
    script_file_path = os.path.join(os.getcwd(), "script")
    workflow_file_path = os.path.join(os.getcwd(), "workflow")
    with open(script_file_path, mode="w", encoding="utf-8") as fh:
        fh.write(script_file_contents)
    with open(workflow_file_path, mode="w", encoding="utf-8") as fh:
        fh.write(workflow_file_contents)

    with open("script.py", mode="w", encoding="utf-8") as fh:
        fh.write(
            dedent(
                """
                from ert import ErtScript
                class Script(ErtScript):
                    def run(self, *args):
                        pass
                """
            )
        )
    with open("config.ert", mode="w", encoding="utf-8") as fh:
        fh.write(
            dedent(
                f"""
                NUM_REALIZATIONS 1
                DEFINE <ZERO> 0
                LOAD_WORKFLOW_JOB {script_file_path} script
                LOAD_WORKFLOW {workflow_file_path}
                """
            )
        )

    ert_config = ErtConfig.from_file("config.ert")

    assert ert_config is not None


@pytest.mark.usefixtures("use_tmpdir")
def test_validate_job_args_no_warning(caplog, recwarn):
    caplog.set_level(logging.WARNING)

    with open("job_file", "w", encoding="utf-8") as fout:
        fout.write("EXECUTABLE echo\nARGLIST <ECLBASE> <RUNPATH>\n")

    with open("config_file.ert", "w", encoding="utf-8") as fout:
        # Write a minimal config file
        fout.write("NUM_REALIZATIONS 1\n")
        fout.write("INSTALL_JOB job_name job_file\n")
        fout.write(
            "FORWARD_MODEL job_name(<ECLBASE>=A/<ECLBASE>, <RUNPATH>=<RUNPATH>/x)\n"
        )

    ErtConfig.from_file("config_file.ert")

    # Check no warning is logged when config contains
    # forward model with <ECLBASE> and <RUNPATH> as arguments
    assert caplog.text == ""
    for w in recwarn:
        assert not issubclass(w.category, ConfigWarning)


@pytest.mark.usefixtures("use_tmpdir")
def test_validate_no_logs_when_overwriting_with_same_value(caplog):
    with open("job_file", "w", encoding="utf-8") as fout:
        fout.write("EXECUTABLE echo\nARGLIST <VAR1> <VAR2> <VAR3>\n")

    with open("config_file.ert", "w", encoding="utf-8") as fout:
        fout.write("NUM_REALIZATIONS 1\n")
        fout.write("DEFINE <VAR1> 10\n")
        fout.write("DEFINE <VAR2> 20\n")
        fout.write("DEFINE <VAR3> 55\n")
        fout.write("INSTALL_JOB job_name job_file\n")
        fout.write("FORWARD_MODEL job_name(<VAR1>=10, <VAR2>=<VAR2>, <VAR3>=5)\n")

    with caplog.at_level(logging.INFO):
        ert_conf = ErtConfig.from_file("config_file.ert")
        ert_conf.forward_model_data_to_json("0", "0", 0)
    assert (
        "Private arg '<VAR3>':'5' chosen over global '55' in forward model job_name"
        in caplog.text
        and "Private arg '<VAR1>':'10' chosen over global '10' in forward model job_name"
        not in caplog.text
        and "Private arg '<VAR2>':'20' chosen over global '20' in forward model job_name"
        not in caplog.text
    )


@pytest.mark.usefixtures("use_tmpdir")
@pytest.mark.parametrize(
    "obsolete_analysis_keyword,error_msg",
    [
        ("USE_EE", "Keyword USE_EE has been replaced"),
        ("USE_GE", "Keyword USE_GE has been replaced"),
        ("ENKF_NCOMP", r"ENKF_NCOMP keyword\(s\) has been removed"),
        (
            "ENKF_SUBSPACE_DIMENSION",
            r"ENKF_SUBSPACE_DIMENSION keyword\(s\) has been removed",
        ),
    ],
)
def test_that_removed_analysis_module_keywords_raises_error(
    obsolete_analysis_keyword, error_msg
):
    test_config_file_name = "test.ert"
    test_config_contents = dedent(
        """
        NUM_REALIZATIONS  1
        """
    )
    with open(test_config_file_name, "w", encoding="utf-8") as fh:
        fh.write(test_config_contents)
        obsolete_keyword = (
            "ANALYSIS_SET_VAR STD_ENKF " + obsolete_analysis_keyword + " 1"
        )
        fh.write(obsolete_keyword)

    with pytest.raises(
        ConfigValidationError,
        match=error_msg,
    ):
        _ = ErtConfig.from_file(test_config_file_name)


def test_ert_config_parser_fails_gracefully_on_unreadable_config_file(caplog, tmp_path):
    config_file = Path(tmp_path) / "config.ert"
    config_file.write_text("")
    os.chmod(config_file, 0x0)
    caplog.set_level(logging.WARNING)

    with pytest.raises(OSError, match="[Pp]ermission"):
        ErtConfig.from_file(config_file)


@pytest.mark.usefixtures("use_tmpdir")
def test_that_context_types_are_json_serializable():
    bf = ContextBool(val=False, token=None, keyword_token=None)
    bt = ContextBool(val=True, token=None, keyword_token=None)
    i = ContextInt(val=23, token=None, keyword_token=None)
    s = ContextString(val="forty_two", token=None, keyword_token=None)
    fl = ContextFloat(val=4.2, token=None, keyword_token=None)
    cl = ContextList.with_values(None, values=[bf, bt, i, s, fl])

    payload = {
        "context_bool_false": bf,
        "context_bool_true": bt,
        "context_int": i,
        "context_str": s,
        "context_float": fl,
        "context_list": cl,
    }

    with open("test.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, cls=ContextBoolEncoder)

    with open("test.json", "r", encoding="utf-8") as f:
        r = json.load(f)

    assert isinstance(r["context_bool_false"], bool)
    assert isinstance(r["context_bool_true"], bool)
    assert r["context_bool_false"] is False
    assert r["context_bool_true"] is True
    assert isinstance(r["context_int"], int)
    assert isinstance(r["context_str"], str)
    assert isinstance(r["context_float"], float)
    assert isinstance(r["context_list"], list)


@pytest.mark.usefixtures("copy_snake_oil_case")
def test_no_timemap_or_refcase_provides_clear_error():

    with fileinput.input("snake_oil.ert", inplace=True) as fin:
        for line in fin:
            if line.startswith("REFCASE"):
                continue
            if line.startswith("TIME_MAP"):
                continue
            print(line, end="")

    with fileinput.input("observations/observations.txt", inplace=True) as fin:
        for line in fin:
            if line.startswith("HISTORY_OBSERVATION"):
                continue
            print(line, end="")

    with pytest.raises(
        ObservationConfigError,
        match="Missing REFCASE or TIME_MAP for observations: WPR_DIFF_1",
    ):
        ErtConfig.from_file("snake_oil.ert")


@pytest.mark.usefixtures("copy_snake_oil_case")
def test_that_multiple_errors_are_shown_when_validating_observation_config():
    with fileinput.input("observations/observations.txt", inplace=True) as fin:
        for line_number, line in enumerate(fin, 1):
            if line_number in [13, 32]:
                continue
            print(line, end="")

    with pytest.raises(ObservationConfigError) as err:
        _ = ErtConfig.from_file("snake_oil.ert")

    expected_errors = [
        'Line 11 (Column 21-32): Missing item "VALUE" in WOPR_OP1_36',
        'Line 26 (Column 21-33): Missing item "KEY" in WOPR_OP1_108',
    ]

    for error in expected_errors:
        assert error in str(err.value)


@pytest.mark.usefixtures("copy_snake_oil_case")
def test_that_multiple_errors_are_shown_when_generating_observations():
    injected_errors = {
        7: "    RESTART = 0;",
        15: "    DATE    = 2010-01-01;",
        31: "    DATE    = 2009-12-15;",
        39: "    DATE    = 2010-12-10;",
    }
    with fileinput.input("observations/observations.txt", inplace=True) as fin:
        for line_number, line in enumerate(fin, 1):
            if line_number in injected_errors:
                print(injected_errors[line_number])
                continue
            print(line, end="")

    with pytest.raises(ObservationConfigError) as err:
        _ = ErtConfig.from_file("snake_oil.ert")

    expected_errors = [
        "Line 3 (Column 21-31): It is unfortunately not possible",
        "Line 11 (Column 21-32): It is unfortunately not possible",
        "Line 27 (Column 21-33): Problem with date in summary observation WOPR_OP1_108",
        "Line 35 (Column 21-33): Problem with date in summary observation WOPR_OP1_144",
    ]

    for error in expected_errors:
        assert error in str(err.value)


def test_job_name_with_slash_fails_validation(tmp_path):
    config_file = Path(tmp_path) / "config.ert"
    config_file.write_text("NUM_REALIZATIONS 100\nJOBNAME dir/eclbase")

    with pytest.raises(ConfigValidationError, match="JOBNAME cannot contain '/'"):
        ErtConfig.from_file(str(config_file))


def test_using_relative_path_to_eclbase_sets_jobname_to_basename(tmp_path):
    config_file = Path(tmp_path) / "config.ert"
    config_file.write_text("NUM_REALIZATIONS 100\nECLBASE dir/eclbase_%d")

    assert (
        ErtConfig.from_file(str(config_file)).model_config.jobname_format_string
        == "eclbase_<IENS>"
    )
