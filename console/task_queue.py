import queue
import threading
import traceback
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class QueueSnapshot:
    worker_count: int
    queued_count: int
    processing_count: int
    queued_ids: tuple[int, ...]
    processing_ids: tuple[int, ...]


class DocumentTaskQueue:
    def __init__(self, worker_count: int, handler: Callable[[int], None]) -> None:
        self.worker_count = max(1, min(int(worker_count), 10))
        self.handler = handler
        self._queue: queue.Queue[int | None] = queue.Queue()
        self._lock = threading.Lock()
        self._queued_ids: set[int] = set()
        self._processing_ids: set[int] = set()
        self._workers: list[threading.Thread] = []
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._workers:
            return
        for index in range(self.worker_count):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"doc-worker-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)

    def stop(self) -> None:
        self._stop_event.set()
        for _ in self._workers:
            self._queue.put(None)
        for worker in self._workers:
            worker.join(timeout=2)
        self._workers.clear()

    def enqueue(self, document_id: int) -> bool:
        with self._lock:
            if document_id in self._queued_ids or document_id in self._processing_ids:
                return False
            self._queued_ids.add(document_id)
        self._queue.put(document_id)
        return True

    def snapshot(self) -> QueueSnapshot:
        with self._lock:
            return QueueSnapshot(
                worker_count=self.worker_count,
                queued_count=len(self._queued_ids),
                processing_count=len(self._processing_ids),
                queued_ids=tuple(sorted(self._queued_ids)),
                processing_ids=tuple(sorted(self._processing_ids)),
            )

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                document_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if document_id is None:
                self._queue.task_done()
                break

            with self._lock:
                self._queued_ids.discard(document_id)
                self._processing_ids.add(document_id)

            try:
                self.handler(document_id)
            except Exception:
                print(f"[doc-worker] document_id={document_id} failed")
                print(traceback.format_exc())
            finally:
                with self._lock:
                    self._processing_ids.discard(document_id)
                self._queue.task_done()
