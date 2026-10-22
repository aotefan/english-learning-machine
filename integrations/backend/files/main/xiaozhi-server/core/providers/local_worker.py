"""One persistent inference child. A native-model timeout can be terminated safely."""
import atexit
import multiprocessing as mp
import threading
import time


def _serve(pipe):
    from core.providers.local_models import LocalModels
    models = LocalModels()
    while True:
        try:
            args = pipe.recv()
        except EOFError:
            break
        if args is None:
            break  # Cooperative shutdown lets multiprocessing finalizers run.
        try:
            start = time.perf_counter()
            value = models.run(*args)
            pipe.send((True, value, (time.perf_counter()-start)*1000))
        except Exception as exc:
            pipe.send((False, f'{type(exc).__name__}: {exc}', None))
    pipe.close()


class ModelWorker:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.pipe = None

    def close(self, force=False):
        result = None
        if self.process is not None:
            if not force and self.process.is_alive() and self.pipe is not None:
                try:
                    self.pipe.send(None)
                except (EOFError, OSError):
                    pass
                self.process.join(timeout=2)
            forced = self.process.is_alive()
            if forced:
                self.process.terminate()
                self.process.join(timeout=2)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=2)
            result = {'pid': self.process.pid, 'exit_code': self.process.exitcode,
                      'forced': forced}
            self.process.close()
            self.process = None
        if self.pipe is not None:
            self.pipe.close()
            self.pipe = None
        return result

    def call(self, stage, value, config):
        timeout = float(config.get('timeout_s', 90))
        if timeout <= 0:
            raise ValueError('timeout_s must be positive')
        deadline = time.monotonic() + timeout
        if not self.lock.acquire(timeout=timeout):
            # A disconnected request may still be finishing native inference.
            # Waiting for it must not bypass the next request's stage budget.
            raise TimeoutError(f'{stage}_queue_timeout')
        try:
            if self.process is None or not self.process.is_alive():
                self.close()
                parent, child = mp.get_context('spawn').Pipe()
                self.process = mp.get_context('spawn').Process(target=_serve, args=(child,), daemon=True)
                self.process.start()
                child.close()
                self.pipe = parent
            try:
                self.pipe.send((stage, value, config))
                if not self.pipe.poll(max(0, deadline-time.monotonic())):
                    raise TimeoutError(f'{stage}_timeout')
                ok, result, elapsed_ms = self.pipe.recv()
                if not ok:
                    raise RuntimeError(result)
                return result
            except (TimeoutError, EOFError, OSError):
                self.close(force=True)
                raise
        finally:
            self.lock.release()


worker = ModelWorker()
atexit.register(worker.close)
