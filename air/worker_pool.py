# SPDX-License-Identifier: GPL-2.0

"""Worker pool managing setup and LLM worker threads"""

from threading import Thread
from .setup_worker import SetupWorker


class WorkerPool:
    """Manages setup and LLM worker threads"""

    def __init__(self, num_setup_workers: int, num_llm_workers: int,
                 config, storage, llm_worker, review_queue, temp_copy_queue, worktree_mgr, patchwork=None):
        """Initialize worker pool

        Args:
            num_setup_workers: Number of setup workers (should equal max_work_trees)
            num_llm_workers: Number of LLM workers (should equal max_claude_runs)
            config: AirConfig instance (for creating SetupWorker instances)
            storage: ReviewStorage instance (for creating SetupWorker instances)
            llm_worker: LLMWorker instance
            review_queue: ReviewQueue instance
            temp_copy_queue: TempCopyQueue instance
            worktree_mgr: WorkTreeManager instance
            patchwork: Optional Patchwork instance
        """
        self.num_setup_workers = num_setup_workers
        self.num_llm_workers = num_llm_workers
        self.config = config
        self.storage = storage
        self.llm_worker = llm_worker
        self.review_queue = review_queue
        self.temp_copy_queue = temp_copy_queue
        self.worktree_mgr = worktree_mgr
        self.patchwork = patchwork
        self.running = False
        self.threads = []

    def start(self):
        """Start all worker threads"""
        self.running = True

        # Start setup workers (one per work tree)
        for i in range(self.num_setup_workers):
            wt_id = i + 1  # Work tree IDs are 1-based

            # Create a dedicated SetupWorker instance for this thread
            setup_worker = SetupWorker(
                self.config,
                self.worktree_mgr,
                self.storage,
                self.temp_copy_queue,
                wt_id,
                self.patchwork
            )

            thread = Thread(
                target=setup_worker.worker_loop,
                args=(i + 1, self.review_queue),
                name=f"Setup-{i+1}",
                daemon=True
            )
            thread.start()
            self.threads.append(thread)
            print(f"Started setup worker thread {thread.name} with work tree {wt_id}")

        # Start LLM workers
        for i in range(self.num_llm_workers):
            thread = Thread(
                target=self.llm_worker.worker_loop,
                args=(i + 1, self.temp_copy_queue),
                name=f"LLM-{i+1}",
                daemon=True
            )
            thread.start()
            self.threads.append(thread)
            print(f"Started LLM worker thread {thread.name}")

        print(f"Worker pool started: {self.num_setup_workers} setup workers, {self.num_llm_workers} LLM workers")

    def stop(self):
        """Stop all worker threads"""
        self.running = False
        for thread in self.threads:
            thread.join(timeout=5)
