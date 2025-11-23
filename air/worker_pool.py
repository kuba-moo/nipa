# SPDX-License-Identifier: GPL-2.0

"""Worker pool managing setup and LLM worker threads"""

from threading import Thread



class WorkerPool:
    """Manages setup and LLM worker threads"""

    def __init__(self, num_setup_workers: int, num_llm_workers: int,
                 setup_worker, llm_worker, review_queue, temp_copy_queue, worktree_mgr):
        """Initialize worker pool

        Args:
            num_setup_workers: Number of setup workers (should equal max_work_trees)
            num_llm_workers: Number of LLM workers (should equal max_claude_runs)
            setup_worker: SetupWorker instance
            llm_worker: LLMWorker instance
            review_queue: ReviewQueue instance
            temp_copy_queue: TempCopyQueue instance
            worktree_mgr: WorkTreeManager instance
        """
        self.num_setup_workers = num_setup_workers
        self.num_llm_workers = num_llm_workers
        self.setup_worker = setup_worker
        self.llm_worker = llm_worker
        self.review_queue = review_queue
        self.temp_copy_queue = temp_copy_queue
        self.worktree_mgr = worktree_mgr
        self.running = False
        self.threads = []

    def start(self):
        """Start all worker threads"""
        self.running = True

        # Start setup workers (one per work tree)
        for i in range(self.num_setup_workers):
            wt_id = i + 1  # Work tree IDs are 1-based
            thread = Thread(
                target=self.setup_worker.worker_loop,
                args=(i + 1, wt_id, self.review_queue),
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
