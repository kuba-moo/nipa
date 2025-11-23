# SPDX-License-Identifier: GPL-2.0

"""Queue for temporary work tree copies ready for LLM processing"""

import threading
from queue import Queue, Empty
from typing import Optional, Dict


class TempCopyQueue:
    """Queue for passing temp work tree copies from setup workers to LLM workers"""

    def __init__(self, max_size: int):
        """Initialize temp copy queue

        Args:
            max_size: Maximum number of temp copies that can be queued
                     (prevents setup workers from getting too far ahead)
        """
        self.queue = Queue(maxsize=max_size)
        self.lock = threading.Lock()

    def put(self, temp_copy_info: Dict, block: bool = True, timeout: Optional[float] = None):
        """Add a temp copy to the queue

        Args:
            temp_copy_info: Dictionary with temp copy information:
                - temp_path: Path to temporary work tree copy
                - token: Authentication token
                - review_id: Review ID
                - patch_num: Patch number (1-based)
                - commit_hash: Commit hash to review
            block: If True, block if queue is full
            timeout: Timeout for blocking put
        """
        self.queue.put(temp_copy_info, block=block, timeout=timeout)

    def get(self, timeout: Optional[float] = None) -> Optional[Dict]:
        """Get a temp copy from the queue

        Args:
            timeout: Timeout in seconds (None = block forever)

        Returns:
            Temp copy info dictionary or None if timeout
        """
        try:
            return self.queue.get(timeout=timeout)
        except Empty:
            return None

    def size(self) -> int:
        """Get current queue size

        Returns:
            Number of items in queue
        """
        return self.queue.qsize()

    def task_done(self):
        """Mark a task as done (for queue.join())"""
        self.queue.task_done()
