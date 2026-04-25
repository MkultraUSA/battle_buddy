import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from audio_receiver import process_call_audio, _MAX_PROCESS_THREADS

# Bounded queue for incoming audio tasks to prevent thread explosion
_processing_queue = queue.Queue(maxsize=1000)

def worker():
    """Main worker loop to consume audio processing tasks."""
    while True:
        task = _processing_queue.get()
        try:
            # We pass the task details (wav_bytes, metadata) to the main process logic
            process_call_audio(task)
        except Exception as e:
            print(f"[queue] worker error: {e}", flush=True)
        finally:
            _processing_queue.task_done()

def start_queue():
    """Start pool of workers."""
    executor = ThreadPoolExecutor(max_workers=_MAX_PROCESS_THREADS)
    for _ in range(_MAX_PROCESS_THREADS):
        executor.submit(worker)

def enqueue_call(task_data):
    """Try to add a call to the queue. Returns False if full."""
    try:
        _processing_queue.put(task_data, block=False)
        return True
    except queue.Full:
        return False
