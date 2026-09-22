"""Background work the Application starts: one flag per task, claimed under a lock."""
import threading

from ..errors import MarqueeError


def describe_error(error):
    """What a task's `error` says: our own messages as they are, anything else with
    its type, since "timed out" alone does not say what did."""
    if isinstance(error, MarqueeError):
        return str(error)
    return f"{type(error).__name__}: {error}"


class TasksMixin:
    """Part of `Application`; see marquee.web.application."""

    def _claim(self, task, **fields):
        """Mark `task` running if it is not already; False when it was."""
        with self._task_lock:
            if task.get("running"):
                return False
            task.update(fields)
            task["running"] = True
            return True

    def _start(self, task, work, finish=None, **fields):
        """Claim `task` and run `work()` in a thread. False when it was already running.

        Every task used to carry its own copy of this, and the copies drifted: one
        caught only MarqueeError, so an unpacking error killed its thread with no word
        to anyone; one caught nothing. Here whatever `work` raises lands in
        task["error"], `finish` runs whatever happened, and `running` always drops.
        """
        if not self._claim(task, **fields):
            return False

        def run():
            try:
                work()
            except Exception as error:  # noqa: BLE001 - reported on the task
                task["error"] = describe_error(error)
            finally:
                try:
                    if finish is not None:
                        finish()
                finally:
                    task["running"] = False

        threading.Thread(target=run, daemon=True).start()
        return True
