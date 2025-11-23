# SPDX-License-Identifier: GPL-2.0

"""Review queue management with persistence"""

import json
from queue import Queue, Empty
from threading import Lock
from typing import Dict, Optional


class ReviewQueue:
    """Thread-safe review queue with persistence"""

    def __init__(self, queue_path: str):
        """Initialize review queue

        Args:
            queue_path: Path to queue.json file
        """
        self.queue_path = queue_path
        self.queue = Queue()
        self.lock = Lock()
        self.queued_items = []  # List for persistence

        # Load existing queue
        self.load_queue()

    def load_queue(self):
        """Load queue from disk"""
        try:
            with open(self.queue_path, 'r') as f:
                items = json.load(f)
                for item in items:
                    self.queue.put(item)
                    self.queued_items.append(item)
        except FileNotFoundError:
            pass

    def save_queue(self):
        """Save queue to disk (assumes lock is already held)"""
        with open(self.queue_path, 'w') as f:
            json.dump(self.queued_items, f, indent=2)

    def put(self, item: Dict):
        """Add item to queue

        Args:
            item: Review request item
        """
        print(f"[queue.put] Adding item to queue: {item.get('review_id')}")
        self.queue.put(item)
        print("[queue.put] Item added to internal queue")
        with self.lock:
            self.queued_items.append(item)
            self.save_queue()
            print("[queue.put] Acquired lock, appended to queued_items")

    def get(self, timeout: Optional[float] = None) -> Optional[Dict]:
        """Get item from queue

        Args:
            timeout: Timeout in seconds (None = block forever, 0 = non-blocking)

        Returns:
            Review request item or None if queue is empty (when timeout=0)
        """
        try:
            item = self.queue.get(timeout=timeout if timeout is not None else None)
            with self.lock:
                if item in self.queued_items:
                    self.queued_items.remove(item)
                    self.save_queue()
            return item
        except Empty:
            return None

    def get_position(self, review_id: str) -> Optional[int]:
        """Get position of a review in the queue

        Args:
            review_id: Review ID to find

        Returns:
            Number of items ahead in queue, or None if not found
        """
        with self.lock:
            for i, item in enumerate(self.queued_items):
                if item['review_id'] == review_id:
                    return i
            return None

    def get_patch_count_ahead(self, review_id: str) -> int:
        """Get number of patches ahead of a review in the queue

        Args:
            review_id: Review ID to find

        Returns:
            Number of patches ahead in queue
        """
        with self.lock:
            # Find position
            position = None
            for i, item in enumerate(self.queued_items):
                if item['review_id'] == review_id:
                    position = i
                    break

            if position is None:
                return 0

            # Count patches ahead
            patch_count = 0
            for i in range(position):
                patch_count += self.queued_items[i].get('patch_count', 1)

            return patch_count

    def size(self) -> int:
        """Get current queue size

        Returns:
            Number of items in queue
        """
        return self.queue.qsize()
