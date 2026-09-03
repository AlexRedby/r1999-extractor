"""Latest-result-wins Qt worker for bounded UI operations."""

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _TaskSignals(QObject):
    finished = Signal(int, object, object)


class _Task(QRunnable):
    def __init__(self, serial, function, arguments, signals):
        super().__init__()
        self.serial = serial
        self.function = function
        self.arguments = arguments
        self.signals = signals

    def run(self):
        try:
            result = self.function(*self.arguments)
        except Exception as error:
            self.signals.finished.emit(self.serial, None, error)
        else:
            self.signals.finished.emit(self.serial, result, None)


class LatestTaskRunner(QObject):
    """Run blocking calls while accepting only the latest request identity."""

    finished = Signal(object, object)

    def __init__(self, parent=None, *, thread_pool=None):
        super().__init__(parent)
        self.thread_pool = thread_pool or QThreadPool.globalInstance()
        self._serial = 0
        self._active = False
        # The runnable keeps this emitter alive if its dialog closes mid-task.
        self._signals = _TaskSignals()
        self._signals.finished.connect(self._task_finished)

    @property
    def active(self):
        return self._active

    def start(self, function, *arguments):
        self._serial += 1
        self._active = True
        self.thread_pool.start(_Task(self._serial, function, arguments, self._signals))

    def cancel(self):
        if not self._active:
            return False
        self._serial += 1
        self._active = False
        return True

    def _task_finished(self, serial, result, error):
        if serial != self._serial or not self._active:
            return
        self._active = False
        self.finished.emit(result, error)
