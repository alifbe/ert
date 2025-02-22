from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import shutil
from pathlib import Path
from typing import (
    Dict,
    List,
    Literal,
    Mapping,
    MutableMapping,
    Optional,
    Tuple,
    Union,
    get_args,
)

from pydantic import BaseModel, Field
from typing_extensions import Annotated

from ert.scheduler.driver import Driver
from ert.scheduler.event import Event, FinishedEvent, StartedEvent

_POLL_PERIOD = 2.0  # seconds

logger = logging.getLogger(__name__)

JobState = Literal[
    "EXIT", "DONE", "PEND", "RUN", "ZOMBI", "PDONE", "SSUSP", "USUSP", "UNKWN"
]


class FinishedJob(BaseModel):
    job_state: Literal["DONE", "EXIT"]


class QueuedJob(BaseModel):
    job_state: Literal["PEND"]


class RunningJob(BaseModel):
    job_state: Literal["RUN"]


AnyJob = Annotated[
    Union[FinishedJob, QueuedJob, RunningJob], Field(discriminator="job_state")
]

LSF_INFO_JSON_FILENAME = "lsf_info.json"


class _Stat(BaseModel):
    jobs: Mapping[str, AnyJob]


def parse_bjobs(bjobs_output: str) -> Dict[str, Dict[str, Dict[str, str]]]:
    data: Dict[str, Dict[str, str]] = {}
    for line in bjobs_output.splitlines():
        if not line or not line[0].isdigit():
            continue
        tokens = line.split(maxsplit=3)
        if len(tokens) >= 3 and tokens[0] and tokens[2]:
            if tokens[2] not in get_args(JobState):
                logger.error(
                    f"Unknown state {tokens[2]} obtained from "
                    f"LSF for jobid {tokens[0]}, ignored."
                )
                continue
            data[tokens[0]] = {"job_state": tokens[2]}
    return {"jobs": data}


class LsfDriver(Driver):
    def __init__(
        self,
        queue_name: Optional[str] = None,
        bsub_cmd: Optional[str] = None,
        bjobs_cmd: Optional[str] = None,
        bkill_cmd: Optional[str] = None,
    ) -> None:
        super().__init__()

        self._queue_name = queue_name

        self._bsub_cmd = Path(bsub_cmd or shutil.which("bsub") or "bsub")
        self._bjobs_cmd = Path(bjobs_cmd or shutil.which("bjobs") or "bjobs")
        self._bkill_cmd = Path(bkill_cmd or shutil.which("bkill") or "bkill")

        self._jobs: MutableMapping[str, Tuple[int, JobState]] = {}
        self._iens2jobid: MutableMapping[int, str] = {}
        self._max_attempt: int = 100
        self._retry_sleep_period = 3

        self._poll_period = _POLL_PERIOD

    async def submit(
        self,
        iens: int,
        executable: str,
        /,
        *args: str,
        name: str = "dummy",
        runpath: Optional[str] = None,
    ) -> None:
        arg_queue_name = ["-q", self._queue_name] if self._queue_name else []

        bsub_with_args: List[str] = (
            [str(self._bsub_cmd)] + arg_queue_name + ["-J", name, executable, *args]
        )
        logger.debug(f"Submitting to LSF with command {shlex.join(bsub_with_args)}")
        process = await asyncio.create_subprocess_exec(
            *bsub_with_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            logger.error(
                f"Command \"{' '.join(bsub_with_args)}\" failed with "
                f"returncode {process.returncode} and error message: "
                f"{stderr.decode(errors='ignore') or '<empty>'}"
            )
            raise RuntimeError(stderr.decode(errors="ignore"))

        stdout_decoded = stdout.decode(errors="ignore")

        match = re.search("Job <([0-9]+)> is submitted to .+ queue", stdout_decoded)
        if match is None:
            raise RuntimeError(f"Could not understand '{stdout_decoded}' from bsub")
        job_id = match[1]
        logger.info(f"Realization {iens} accepted by LSF, got id {job_id}")

        if runpath is not None:
            (Path(runpath) / LSF_INFO_JSON_FILENAME).write_text(
                json.dumps({"job_id": job_id}), encoding="utf-8"
            )
        self._jobs[job_id] = (iens, "PEND")
        self._iens2jobid[iens] = job_id

    async def kill(self, iens: int) -> None:
        if iens not in self._iens2jobid:
            logger.error(f"LSF kill failed due to missing jobid for realization {iens}")
            return

        job_id = self._iens2jobid[iens]

        logger.debug(f"Killing realization {iens} with LSF-id {job_id}")
        process = await asyncio.create_subprocess_exec(
            self._bkill_cmd,
            job_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            logger.error(
                f"LSF kill failed with returncode {process.returncode} "
                f"and error message {stderr.decode(errors='ignore')}"
            )
            return

        if not re.match(
            f"Job <{job_id}> is being terminated", stdout.decode(errors="ignore")
        ):
            logger.error(
                "LSF kill failed with stdout: "
                + stdout.decode(errors="ignore")
                + " and stderr: "
                + stderr.decode(errors="ignore")
            )
            return

    async def poll(self) -> None:
        while True:
            if not self._jobs.keys():
                await asyncio.sleep(self._poll_period)
                continue
            process = await asyncio.create_subprocess_exec(
                self._bjobs_cmd,
                *self._jobs.keys(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()
            if process.returncode:
                # bjobs may give nonzero return code even when it is providing
                # at least some correct information
                logger.warning(
                    f"bjobs gave returncode {process.returncode} and error {stderr.decode()}"
                )
            stat = _Stat(**parse_bjobs(stdout.decode(errors="ignore")))
            for job_id, job in stat.jobs.items():
                if job_id not in self._jobs:
                    continue

                iens, old_state = self._jobs[job_id]
                new_state = job.job_state
                if old_state == new_state:
                    continue

                self._jobs[job_id] = (iens, new_state)
                event: Optional[Event] = None
                if isinstance(job, RunningJob):
                    logger.debug(f"Realization {iens} is running.")
                    event = StartedEvent(iens=iens)
                elif isinstance(job, FinishedJob):
                    aborted = job.job_state == "EXIT"
                    event = FinishedEvent(
                        iens=iens,
                        returncode=1 if job.job_state == "EXIT" else 0,
                        aborted=aborted,
                    )
                    if aborted:
                        logger.warning(
                            f"Realization {iens} (LSF-id: {self._iens2jobid[iens]}) failed."
                        )
                    else:
                        logger.info(
                            f"Realization {iens} (LSF-id: {self._iens2jobid[iens]}) succeeded"
                        )
                    del self._jobs[job_id]
                    del self._iens2jobid[iens]

                if event:
                    await self.event_queue.put(event)

            missing_in_bjobs_output = set(self._jobs) - set(stat.jobs.keys())
            if missing_in_bjobs_output:
                logger.warning(
                    f"bjobs did not give status for job_ids {missing_in_bjobs_output}"
                )
            await asyncio.sleep(_POLL_PERIOD)

    async def finish(self) -> None:
        pass
