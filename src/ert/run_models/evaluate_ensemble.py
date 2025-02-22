from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from ert.ensemble_evaluator import EvaluatorServerConfig
from ert.run_context import RunContext
from ert.run_models.run_arguments import EvaluateEnsembleRunArguments
from ert.storage import EnsembleAccessor, StorageAccessor

from . import BaseRunModel

if TYPE_CHECKING:
    from ert.config import ErtConfig, QueueConfig


# pylint: disable=too-many-arguments
class EvaluateEnsemble(BaseRunModel):
    """
    This workflow will evaluate ensembles which have parameters, but no
    simulation has been performed, so there are no responses. This can
    be used in instances where the parameters are sampled manually, or
    after performing a manual update step. This will always read parameter
    and response configuration from the stored ensemble, and will not
    reflect any changes to the user configuration on disk.
    """

    def __init__(
        self,
        simulation_arguments: EvaluateEnsembleRunArguments,
        config: ErtConfig,
        storage: StorageAccessor,
        queue_config: QueueConfig,
    ):
        super().__init__(
            simulation_arguments,
            config,
            storage,
            queue_config,
        )

    def run_experiment(
        self,
        evaluator_server_config: EvaluatorServerConfig,
    ) -> RunContext:
        self.setPhaseName("Running evaluate experiment...", indeterminate=False)
        ensemble_name = self.simulation_arguments.current_case
        ensemble = self._storage.get_ensemble_by_name(ensemble_name)
        assert isinstance(ensemble, EnsembleAccessor)
        experiment = ensemble.experiment
        self.set_env_key("_ERT_EXPERIMENT_ID", str(experiment.id))
        self.set_env_key("_ERT_ENSEMBLE_ID", str(ensemble.id))

        prior_context = RunContext(
            sim_fs=ensemble,
            runpaths=self.run_paths,
            initial_mask=np.array(
                self._simulation_arguments.active_realizations, dtype=bool
            ),
            iteration=ensemble.iteration,
        )

        iteration = prior_context.iteration
        phase_count = iteration + 1
        self.setPhaseCount(phase_count)
        self._evaluate_and_postprocess(prior_context, evaluator_server_config)

        self.setPhase(phase_count, "Simulations completed.")

        return prior_context

    @property
    def simulation_arguments(self) -> EvaluateEnsembleRunArguments:
        assert isinstance(self._simulation_arguments, EvaluateEnsembleRunArguments)
        return self._simulation_arguments

    @classmethod
    def name(cls) -> str:
        return "Evaluate ensemble"
