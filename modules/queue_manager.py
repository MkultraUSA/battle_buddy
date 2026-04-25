import queue
from concurrent.futures import ThreadPoolExecutor
import logging

class QueueManager:
    def __init__(self, max_size=10):
        self.queue = queue.Queue(maxsize=max_size)
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.logger = logging.getLogger(__name__)

    def start_queue(self, process_fn):
        def worker():
            while True:
                task = self.queue.get()
                if task is None: break
                try:
                    process_fn(task)
                except Exception as e:
                    self.logger.error(f"Error processing task: {e}")
                finally:
                    self.queue.task_done()
        self.executor.submit(worker)

    def enqueue_call(self, task):
        try:
            self.queue.put_nowait(task)
        except queue.Full:
            self.logger.error("Queue full, dropping task")

queue_manager = QueueManager()
