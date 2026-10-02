"""
Executor threads for ``run_database_tasks --threads M``.

The main thread of the worker stays the one that fetches and receives: it
is the only thread polling the database, the only one on the broker's
connection, and the one the signal handlers run in. What ``--threads``
changes is that a task it found is run by one of M executor threads instead
of inline, so M tasks are in progress at once while there is still one
poller per process.

Each executor has a worker id of its own, derived from the process's, so
the ``worker_ids`` recorded on a task say which thread ran it. Django keeps
database connections per thread, so each executor has a connection of its
own too; after every task it closes the ones that outlived ``CONN_MAX_AGE``
or broke, the way a request does, since a worker has no ``request_finished``
to do it for it.

Tasks run under ``--threads`` share one process. A task that is not
thread-safe, calls ``os._exit()`` or crashes the interpreter takes the other
M-1 with it; that is the trade the option makes, and the README says so.
"""

import logging
import queue
import threading

from django.db import close_old_connections, connections

logger = logging.getLogger("django_database_task")


class ExecutorThreads:
    """
    M threads that run the jobs the main thread hands them.

    Only the main thread calls :meth:`acquire`, :meth:`submit`,
    :meth:`release` and :meth:`collect`; the executors put themselves back
    on the idle list and their outcomes on the results queue. A job is a
    callable taking no arguments; what it returns is handed back by
    :meth:`collect`.

    Args:
        count: How many executors.
        worker_id: The process's worker id; executor ``n`` is
            ``f"{worker_id}-t{n}"``.
    """

    def __init__(self, count, worker_id):
        if count < 1:
            raise ValueError("count must be at least 1")
        self.count = count
        self.worker_ids = [f"{worker_id}-t{n}" for n in range(1, count + 1)]
        self._inboxes = [queue.Queue(maxsize=1) for _ in range(count)]
        self._idle = queue.Queue()
        for index in range(count):
            self._idle.put(index)
        self._results = queue.Queue()
        #: Jobs handed out and not yet collected. Main thread only.
        self._busy = 0
        self._threads = [
            threading.Thread(
                target=self._run,
                args=(index,),
                name=f"executor-{index + 1}",
                daemon=True,
            )
            for index in range(count)
        ]

    def start(self):
        for thread in self._threads:
            thread.start()
        return self

    def stop(self):
        """Let every executor finish its job and exit, and wait for them."""
        for inbox in self._inboxes:
            inbox.put(None)
        for thread in self._threads:
            thread.join()

    @property
    def busy(self):
        """How many jobs are out: submitted and not yet collected."""
        return self._busy

    def idle_count(self):
        """How many executors are free to take a job right now."""
        return self._idle.qsize()

    def acquire(self):
        """
        Take an idle executor, returning its index, or None if none is idle.

        The index is held until :meth:`submit` hands it a job or
        :meth:`release` gives it back, so the caller can do work in between
        (claim a task with the executor's worker id) knowing the slot is its.
        """
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            return None

    def release(self, index):
        """Give back an executor taken with :meth:`acquire` and not used."""
        self._idle.put(index)

    def submit(self, index, job):
        """Hand ``job`` to the executor taken with :meth:`acquire`."""
        self._busy += 1
        self._inboxes[index].put(job)

    def collect(self, timeout=None):
        """
        Return the outcomes of the jobs that have finished since last time.

        With ``timeout``, wait up to that many seconds for the first one when
        none has finished yet; without it, return at once.
        """
        outcomes = []
        try:
            if timeout:
                outcomes.append(self._results.get(timeout=timeout))
            while True:
                outcomes.append(self._results.get_nowait())
        except queue.Empty:
            pass
        self._busy -= len(outcomes)
        return outcomes

    def _run(self, index):
        inbox = self._inboxes[index]
        try:
            while True:
                job = inbox.get()
                if job is None:
                    break
                try:
                    outcome = job()
                except BaseException:
                    # The jobs report their own failures; this is a last
                    # resort so that a slot is never lost to a thread that
                    # died, and so that SystemExit raised by task code ends
                    # the task rather than the thread.
                    logger.exception(
                        "Executor thread %s failed",
                        self.worker_ids[index],
                        extra={"worker_id": self.worker_ids[index]},
                    )
                    outcome = None
                finally:
                    close_old_connections()
                self._results.put(outcome)
                self._idle.put(index)
        finally:
            connections.close_all()
