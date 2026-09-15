"""How the pipeline talks back to whatever is driving it.

Nothing below the CLI prints. The pipeline emits messages and progress to a Reporter;
a terminal implementation writes them out, a web UI would queue them, and tests
collect them.
"""
import sys


class Reporter:
    """Default implementation: silent. Override what you care about."""

    def info(self, message):
        """Something worth telling the user about."""

    def warn(self, message):
        """Something that did not stop the run but may be wrong."""

    def progress(self, stage, done, total, detail=""):
        """Called repeatedly during a long stage. `total` may be 0 when unknown."""

    def stage(self, name):
        """A new phase is starting."""


class CollectingReporter(Reporter):
    """Keeps everything, for tests and for a UI that renders after the fact."""

    def __init__(self):
        self.messages = []
        self.warnings = []
        self.stages = []
        self.last_progress = None

    def info(self, message):
        self.messages.append(message)

    def warn(self, message):
        self.warnings.append(message)
        self.messages.append(f"Warning: {message}")

    def progress(self, stage, done, total, detail=""):
        self.last_progress = (stage, done, total, detail)

    def stage(self, name):
        self.stages.append(name)

    @property
    def text(self):
        return "\n".join(self.messages)


class TerminalReporter(Reporter):
    """Prints, and throttles progress so a 45,000 item loop does not flood the screen."""

    def __init__(self, stream=None, step=2, quiet=False):
        self.stream = stream or sys.stdout
        self.step = step
        self.quiet = quiet
        self._last_percent = {}

    def _write(self, text):
        print(text, file=self.stream)

    def info(self, message):
        if not self.quiet:
            self._write(message)

    def warn(self, message):
        self._write(f"Warning: {message}")

    def stage(self, name):
        self._last_percent.pop(name, None)
        if not self.quiet:
            self._write(name)

    def progress(self, stage, done, total, detail=""):
        if self.quiet or total <= 0:
            return
        percent = int(done / total * 100)
        if percent < self._last_percent.get(stage, -self.step - 1) + self.step:
            return
        self._last_percent[stage] = percent
        suffix = f" {detail}" if detail else ""
        self._write(f"{percent}% ({done}/{total}){suffix}")
