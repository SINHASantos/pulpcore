from gettext import gettext as _

import logging
import os
import random
import select
import signal
import socket
import contextlib
from datetime import datetime, timedelta
from multiprocessing import Process
from tempfile import TemporaryDirectory
from packaging.version import parse as parse_version

from django.conf import settings
from django.db import connection
from django.db.models import Case, Count, F, Max, Value, When
from django.db.models.functions import Random
from django.utils import timezone

from pulpcore.constants import (
    TASK_STATES,
    TASK_INCOMPLETE_STATES,
    TASK_SCHEDULING_LOCK,
    TASK_UNBLOCKING_LOCK,
    TASK_METRICS_HEARTBEAT_LOCK,
    TASK_WAKEUP_UNBLOCK,
    TASK_WAKEUP_HANDLE,
)
from pulpcore.metrics import init_otel_meter
from pulpcore.app.apps import pulp_plugin_configs
from pulpcore.app.models import Worker, Task, ApiAppStatus, ContentAppStatus
from pulpcore.app.util import PGAdvisoryLock, get_domain
from pulpcore.exceptions import AdvisoryLockError

from pulpcore.tasking.storage import WorkerDirectory
from pulpcore.tasking._util import (
    delete_incomplete_resources,
    dispatch_scheduled_tasks,
    perform_task,
    startup_hook,
)

_logger = logging.getLogger(__name__)
random.seed()

# The following four constants are current "best guesses".
# Unless/until we can provide reasonable ways to decide to change their values,
# they will live as constants instead of "proper" settings.

# Seconds for a task to finish on semi graceful worker shutdown (approx)
TASK_GRACE_INTERVAL = settings.TASK_GRACE_INTERVAL
# Seconds between attempts to kill the subprocess (approx)
TASK_KILL_INTERVAL = 1
# Number of heartbeats between cleaning up worker processes (approx)
WORKER_CLEANUP_INTERVAL = 100
# Threshold time in seconds of an unblocked task before we consider a queue stalled
THRESHOLD_UNBLOCKED_WAITING_TIME = 5


class PulpcoreWorker:
    def __init__(self, auxiliary=False):
        # Notification states from several signal handlers
        self.shutdown_requested = False
        self.wakeup_unblock = False
        self.wakeup_handle = False
        self.cancel_task = False

        self.auxiliary = auxiliary
        self.task = None
        self.name = f"{os.getpid()}@{socket.getfqdn()}"
        self.heartbeat_period = timedelta(seconds=settings.WORKER_TTL / 3)
        self.last_metric_heartbeat = timezone.now()
        self.versions = {app.label: app.version for app in pulp_plugin_configs()}
        self.cursor = connection.cursor()
        self.worker = self.handle_worker_heartbeat()
        # This defaults to immediate task cancellation.
        # It will be set into the future on moderately graceful worker shutdown,
        # and set to None for fully graceful shutdown.
        self.task_grace_timeout = timezone.now()
        self.worker_cleanup_countdown = random.randint(
            int(WORKER_CLEANUP_INTERVAL / 10), WORKER_CLEANUP_INTERVAL
        )

        # Add a file descriptor to trigger select on signals
        self.sentinel, sentinel_w = os.pipe()
        os.set_blocking(self.sentinel, False)
        os.set_blocking(sentinel_w, False)
        signal.set_wakeup_fd(sentinel_w)

        self._init_instrumentation()

        startup_hook()

    def _init_instrumentation(self):
        if settings.OTEL_ENABLED:
            meter = init_otel_meter("pulp-worker")
            self.tasks_unblocked_queue_meter = meter.create_gauge(
                name="tasks_unblocked_queue",
                description="Number of unblocked tasks waiting in the queue.",
                unit="tasks",
            )
            self.tasks_longest_unblocked_time_meter = meter.create_gauge(
                name="tasks_longest_unblocked_time",
                description="The age of the longest waiting task.",
                unit="seconds",
            )
            self.record_unblocked_waiting_tasks_metric = self._record_unblocked_waiting_tasks_metric
        else:
            self.record_unblocked_waiting_tasks_metric = lambda *args, **kwargs: None

    def _signal_handler(self, thesignal, frame):
        if thesignal in (signal.SIGHUP, signal.SIGTERM):
            _logger.info(_("Worker %s was requested to shut down gracefully."), self.name)
            # Wait forever...
            self.task_grace_timeout = None
        else:
            # Reset signal handlers to default
            # If you kill the process a second time it's not graceful anymore.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)

            _logger.info(_("Worker %s was requested to shut down."), self.name)
            self.task_grace_timeout = timezone.now() + timezone.timedelta(
                seconds=TASK_GRACE_INTERVAL
            )
        self.shutdown_requested = True

    def _pg_notify_handler(self, notification):
        if notification.channel == "pulp_worker_wakeup":
            if notification.payload == TASK_WAKEUP_UNBLOCK:
                # Auxiliary workers don't do this.
                self.wakeup_unblock = not self.auxiliary
            elif notification.payload == TASK_WAKEUP_HANDLE:
                self.wakeup_handle = True
            else:
                _logger.warn("Unknown wakeup call recieved. Reason: '%s'", notification.payload)
                # We cannot be sure so assume everything happened.
                self.wakeup_unblock = not self.auxiliary
                self.wakeup_handle = True

        elif notification.channel == "pulp_worker_metrics_heartbeat":
            self.last_metric_heartbeat = datetime.fromisoformat(notification.payload)
        elif self.task and notification.channel == "pulp_worker_cancel":
            if notification.payload == str(self.task.pk):
                self.cancel_task = True

    def handle_worker_heartbeat(self):
        """
        Create or update worker heartbeat records.

        Existing Worker objects are searched for one to update. If an existing one is found, it is
        updated. Otherwise a new Worker entry is created. Logging at the info level is also done.

        """
        worker, created = Worker.objects.get_or_create(
            name=self.name, defaults={"versions": self.versions}
        )
        if not created and worker.versions != self.versions:
            worker.versions = self.versions
            worker.save(update_fields=["versions"])

        if created:
            _logger.info(_("New worker '{name}' discovered").format(name=self.name))
        elif worker.online is False:
            _logger.info(_("Worker '{name}' is back online.").format(name=self.name))

        worker.save_heartbeat()

        msg = "Worker heartbeat from '{name}' at time {timestamp}".format(
            timestamp=worker.last_heartbeat, name=self.name
        )
        _logger.debug(msg)

        return worker

    def shutdown(self):
        self.worker.delete()
        _logger.info(_("Worker %s was shut down."), self.name)

    def worker_cleanup(self):
        for cls, cls_name in (
            (Worker, "pulp"),
            (ApiAppStatus, "api"),
            (ContentAppStatus, "content"),
        ):
            qs = cls.objects.missing(age=timedelta(days=7))
            if qs:
                for app_worker in qs:
                    _logger.info(_("Clean missing %s worker %s."), cls_name, app_worker.name)
                qs.delete()

    def beat(self):
        if self.worker.last_heartbeat < timezone.now() - self.heartbeat_period:
            self.worker = self.handle_worker_heartbeat()
            if not self.auxiliary:
                self.worker_cleanup_countdown -= 1
                if self.worker_cleanup_countdown <= 0:
                    self.worker_cleanup_countdown = WORKER_CLEANUP_INTERVAL
                    self.worker_cleanup()
                with contextlib.suppress(AdvisoryLockError), PGAdvisoryLock(TASK_SCHEDULING_LOCK):
                    dispatch_scheduled_tasks()
                # This "reporting code" must not me moved inside a task, because it is supposed
                # to be able to report on a congested tasking system to produce reliable results.
                self.record_unblocked_waiting_tasks_metric()

    def notify_workers(self, reason="unknown"):
        self.cursor.execute("SELECT pg_notify('pulp_worker_wakeup', %s)", (reason,))

    def cancel_abandoned_task(self, task, final_state, reason=None):
        """Cancel and clean up an abandoned task.

        This function must only be called while holding the lock for that task. It is a no-op if
        the task is neither in "running" nor "canceling" state.

        Return ``True`` if the task was actually canceled, ``False`` otherwise.
        """
        # A task is considered abandoned when in running state, but no worker holds its lock
        domain = get_domain()
        try:
            task.set_canceling()
        except RuntimeError:
            return False
        if reason:
            _logger.info(
                "Cleaning up task %s in domain: %s and marking as %s. Reason: %s",
                task.pk,
                domain.name,
                final_state,
                reason,
            )
        else:
            _logger.info(
                _("Cleaning up task %s in domain: %s and marking as %s."),
                task.pk,
                domain.name,
                final_state,
            )
        delete_incomplete_resources(task)
        task.set_canceled(final_state=final_state, reason=reason)
        if task.reserved_resources_record:
            self.notify_workers(TASK_WAKEUP_UNBLOCK)
        return True

    def is_compatible(self, task):
        domain = get_domain()
        unmatched_versions = [
            f"task: {label}>={version} worker: {self.versions.get(label)}"
            for label, version in task.versions.items()
            if label not in self.versions
            or parse_version(self.versions[label]) < parse_version(version)
        ]
        if unmatched_versions:
            _logger.info(
                _("Incompatible versions to execute task %s in domain: %s by worker %s: %s"),
                task.pk,
                domain.name,
                self.name,
                ",".join(unmatched_versions),
            )
            return False
        return True

    def unblock_tasks(self):
        """Iterate over waiting tasks and mark them unblocked accordingly.

        This function also handles the communication around it.
        In order to prevent multiple workers to attempt unblocking tasks at the same time it tries
        to acquire a lock and just returns on failure to do so.
        Also it clears the notification about tasks to be unblocked and sends the notification that
        new unblocked tasks are made available.

        Returns the number of new unblocked tasks.
        """

        assert not self.auxiliary

        count = 0
        self.wakeup_unblock_tasks = False
        with contextlib.suppress(AdvisoryLockError), PGAdvisoryLock(TASK_UNBLOCKING_LOCK):
            if count := self._unblock_tasks():
                self.notify_workers(TASK_WAKEUP_HANDLE)
        return count

    def _unblock_tasks(self):
        """Iterate over waiting tasks and mark them unblocked accordingly.

        Returns the number of new unblocked tasks.
        """

        count = 0
        taken_exclusive_resources = set()
        taken_shared_resources = set()
        # When batching this query, be sure to use "pulp_created" as a cursor
        for task in (
            Task.objects.filter(state__in=TASK_INCOMPLETE_STATES)
            .order_by("pulp_created")
            .select_related("pulp_domain")
        ):
            reserved_resources_record = task.reserved_resources_record or []
            exclusive_resources = [
                resource
                for resource in reserved_resources_record
                if not resource.startswith("shared:")
            ]
            shared_resources = [
                resource[7:]
                for resource in reserved_resources_record
                if resource.startswith("shared:") and resource[7:] not in exclusive_resources
            ]
            if task.state == TASK_STATES.CANCELING:
                if task.unblocked_at is None:
                    _logger.debug(
                        "Marking canceling task %s in domain: %s unblocked.",
                        task.pk,
                        task.pulp_domain.name,
                    )
                    task.unblock()
                    count += 1
                # Don't consider this task's resources as held.
                continue

            elif (
                task.state == TASK_STATES.WAITING
                and task.unblocked_at is None
                # No exclusive resource taken?
                and not any(
                    resource in taken_exclusive_resources or resource in taken_shared_resources
                    for resource in exclusive_resources
                )
                # No shared resource exclusively taken?
                and not any(resource in taken_exclusive_resources for resource in shared_resources)
            ):
                _logger.debug(
                    "Marking waiting task %s in domain: %s unblocked.",
                    task.pk,
                    task.pulp_domain.name,
                )
                task.unblock()
                count += 1
            elif task.state == TASK_STATES.RUNNING and task.unblocked_at is None:
                # This should not happen in normal operation.
                # And it is only an issue if the worker running that task died, because it will
                # never be considered for cleanup.
                # But during at least one specific upgrade this situation can emerge.
                # In this case, we can assume that the old algorithm was employed to identify the
                # task as unblocked, and we just rectify the situation here.
                _logger.warning(
                    "Running task %s was not previously marked unblocked. Fixing.", task.pk
                )
                task.unblock()

            # Record the resources of the pending task
            taken_exclusive_resources.update(exclusive_resources)
            taken_shared_resources.update(shared_resources)

        return count

    def iter_tasks(self):
        """Iterate over ready tasks and yield each task while holding the lock."""
        while not self.shutdown_requested:
            # When batching this query, be sure to use "pulp_created" as a cursor
            for task in Task.objects.filter(
                state__in=TASK_INCOMPLETE_STATES,
                unblocked_at__isnull=False,
            ).order_by("-immediate", F("pulp_created") + Value(timedelta(seconds=8)) * Random()):
                # This code will only be called if we acquired the lock successfully
                # The lock will be automatically be released at the end of the block
                with contextlib.suppress(AdvisoryLockError), task:
                    # Check if someone else changed the task before we got the lock
                    task.refresh_from_db()

                    if task.state == TASK_STATES.CANCELING and task.worker is None:
                        # No worker picked this task up before being canceled
                        if self.cancel_abandoned_task(task, TASK_STATES.CANCELED):
                            # Continue looking for the next task without considering this
                            # tasks resources, as we just released them
                            continue
                    if task.state in [TASK_STATES.RUNNING, TASK_STATES.CANCELING]:
                        # A running task without a lock must be abandoned
                        if self.cancel_abandoned_task(
                            task, TASK_STATES.FAILED, "Worker has gone missing."
                        ):
                            # Continue looking for the next task without considering this
                            # tasks resources, as we just released them
                            continue

                    # This statement is using lazy evaluation
                    if (
                        task.state == TASK_STATES.WAITING
                        and task.unblocked_at is not None
                        and self.is_compatible(task)
                    ):
                        yield task
                        # Start from the top of the Task list
                        break
            else:
                # No task found in the for-loop
                break

    def sleep(self):
        """Wait for signals on the wakeup channel while heart beating."""

        _logger.debug(_("Worker %s entering sleep state."), self.name)
        while not self.shutdown_requested and not self.wakeup_handle:
            r, w, x = select.select(
                [self.sentinel, connection.connection], [], [], self.heartbeat_period.seconds
            )
            self.beat()
            if connection.connection in r:
                connection.connection.execute("SELECT 1")
                if self.wakeup_unblock:
                    self.unblock_tasks()
            if self.sentinel in r:
                os.read(self.sentinel, 256)
        _logger.debug(_("Worker %s leaving sleep state."), self.name)

    def supervise_task(self, task):
        """Call and supervise the task process while heart beating.

        This function must only be called while holding the lock for that task."""

        self.cancel_task = False
        self.task = task
        task.worker = self.worker
        task.save(update_fields=["worker"])
        cancel_state = None
        cancel_reason = None
        domain = get_domain()
        with TemporaryDirectory(dir=".") as task_working_dir_rel_path:
            task_process = Process(target=perform_task, args=(task.pk, task_working_dir_rel_path))
            task_process.start()
            while True:
                if cancel_state:
                    if self.task_grace_timeout is None or self.task_grace_timeout > timezone.now():
                        _logger.info("Wait for canceled task to abort.")
                    else:
                        self.task_grace_timeout = timezone.now() + timezone.timedelta(
                            seconds=TASK_KILL_INTERVAL
                        )
                        _logger.info(
                            "Aborting current task %s in domain: %s due to cancellation.",
                            task.pk,
                            domain.name,
                        )
                        os.kill(task_process.pid, signal.SIGUSR1)

                r, w, x = select.select(
                    [self.sentinel, connection.connection, task_process.sentinel],
                    [],
                    [],
                    self.heartbeat_period.seconds,
                )
                self.beat()
                if connection.connection in r:
                    connection.connection.execute("SELECT 1")
                    if self.cancel_task:
                        _logger.info(
                            _("Received signal to cancel current task %s in domain: %s."),
                            task.pk,
                            domain.name,
                        )
                        cancel_state = TASK_STATES.CANCELED
                        self.cancel_task = False
                    if self.wakeup_unblock:
                        self.unblock_tasks()
                if task_process.sentinel in r:
                    if not task_process.is_alive():
                        break
                if self.sentinel in r:
                    os.read(self.sentinel, 256)
                if self.shutdown_requested:
                    if self.task_grace_timeout is None or self.task_grace_timeout > timezone.now():
                        msg = (
                            "Worker shutdown requested, waiting for task {pk} in domain: {name} "
                            "to finish.".format(pk=task.pk, name=domain.name)
                        )

                        _logger.info(msg)
                    else:
                        _logger.info(
                            "Aborting current task %s in domain: %s due to worker shutdown.",
                            task.pk,
                            domain.name,
                        )
                        cancel_state = TASK_STATES.FAILED
                        cancel_reason = "Aborted during worker shutdown."

            task_process.join()
            if not cancel_state and task_process.exitcode != 0:
                _logger.warning(
                    "Task process for %s exited with non zero exitcode %i.",
                    task.pk,
                    task_process.exitcode,
                )
                cancel_state = TASK_STATES.FAILED
                if task_process.exitcode < 0:
                    cancel_reason = "Killed by signal {sig_num}.".format(
                        sig_num=-task_process.exitcode
                    )
                else:
                    cancel_reason = "Task process died unexpectedly with exitcode {code}.".format(
                        code=task_process.exitcode
                    )
            if cancel_state:
                self.cancel_abandoned_task(task, cancel_state, cancel_reason)
        if task.reserved_resources_record:
            self.notify_workers(TASK_WAKEUP_UNBLOCK)
        self.task = None

    def handle_unblocked_tasks(self):
        """Pick and supervise tasks until there are no more available tasks.

        Failing to detect new available tasks can lead to a stuck state, as the workers
        would go to sleep and wouldn't be able to know about the unhandled task until
        an external wakeup event occurs (e.g., new worker startup or new task gets in).
        """
        keep_looping = True
        while keep_looping and not self.shutdown_requested:
            # Clear pending wakeups. We are about to handle them anyway.
            self.wakeup_handle = False
            keep_looping = False
            for task in self.iter_tasks():
                keep_looping = True
                self.supervise_task(task)

    def _record_unblocked_waiting_tasks_metric(self):
        now = timezone.now()
        if now > self.last_metric_heartbeat + self.heartbeat_period:
            with contextlib.suppress(AdvisoryLockError), PGAdvisoryLock(
                TASK_METRICS_HEARTBEAT_LOCK
            ):
                # For performance reasons we aggregate these statistics on a single database call.
                unblocked_tasks_stats = (
                    Task.objects.filter(unblocked_at__isnull=False, state=TASK_STATES.WAITING)
                    .annotate(unblocked_for=Value(timezone.now()) - F("unblocked_at"))
                    .aggregate(
                        longest_unblocked_waiting_time=Max(
                            "unblocked_for", default=timezone.timedelta(0)
                        ),
                        unblocked_tasks_count_gte_threshold=Count(
                            Case(
                                When(
                                    unblocked_for__gte=Value(
                                        timezone.timedelta(seconds=THRESHOLD_UNBLOCKED_WAITING_TIME)
                                    ),
                                    then=1,
                                )
                            )
                        ),
                    )
                )

                self.tasks_unblocked_queue_meter.set(
                    unblocked_tasks_stats["unblocked_tasks_count_gte_threshold"]
                )
                self.tasks_longest_unblocked_time_meter.set(
                    unblocked_tasks_stats["longest_unblocked_waiting_time"].seconds
                )

                self.cursor.execute(f"NOTIFY pulp_worker_metrics_heartbeat, '{str(now)}'")

    def run(self, burst=False):
        with WorkerDirectory(self.name):
            signal.signal(signal.SIGINT, self._signal_handler)
            signal.signal(signal.SIGTERM, self._signal_handler)
            signal.signal(signal.SIGHUP, self._signal_handler)
            # Subscribe to pgsql channels
            connection.connection.add_notify_handler(self._pg_notify_handler)
            self.cursor.execute("LISTEN pulp_worker_cancel")
            self.cursor.execute("LISTEN pulp_worker_metrics_heartbeat")
            if burst:
                if not self.auxiliary:
                    # Attempt to flush the task queue completely.
                    # Stop iteration if no new tasks were found to unblock.
                    while self.unblock_tasks():
                        self.handle_unblocked_tasks()
                self.handle_unblocked_tasks()
            else:
                self.cursor.execute("LISTEN pulp_worker_wakeup")
                while not self.shutdown_requested:
                    # do work
                    if self.shutdown_requested:
                        break
                    self.handle_unblocked_tasks()
                    if self.shutdown_requested:
                        break
                    # rest until notified to wakeup
                    self.sleep()
                self.cursor.execute("UNLISTEN pulp_worker_wakeup")
            self.cursor.execute("UNLISTEN pulp_worker_metrics_heartbeat")
            self.cursor.execute("UNLISTEN pulp_worker_cancel")
            self.shutdown()
