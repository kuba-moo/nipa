# SPDX-License-Identifier: GPL-2.0

"""Main AIR service orchestrator"""

import os
from typing import Dict, List, Optional

from pw import Patchwork

from .storage import ReviewStorage
from .queue import ReviewQueue
from .temp_copy_queue import TempCopyQueue
from .worktree import WorkTreeManager
from .setup_worker import SetupWorker
from .llm_worker import LLMWorker
from .worker_pool import WorkerPool


class AirService:
    """Main AIR service orchestrator"""

    def __init__(self, config):
        """Initialize AIR service

        Args:
            config: AirConfig instance
        """
        self.config = config

        # Initialize components
        self.storage = ReviewStorage(config.results_path)
        self.queue = ReviewQueue(os.path.join(config.results_path, 'queue.json'))
        self.worktree_mgr = WorkTreeManager(config.git_tree, config.max_work_trees)

        # Initialize temp copy queue
        # Max size = 2x the number of LLM workers to allow some buffering
        # but prevent setup workers from getting too far ahead
        max_temp_copies = config.max_claude_runs * 2
        self.temp_copy_queue = TempCopyQueue(max_temp_copies)

        # Initialize Patchwork if configured
        self.patchwork = None
        if config.config.has_section('patchwork'):
            try:
                self.patchwork = Patchwork(config.config)
                print("Patchwork integration enabled")
            except Exception as e:
                print(f"Failed to initialize Patchwork: {e}")

        # Initialize workers
        self.setup_worker = SetupWorker(config, self.worktree_mgr, self.storage,
                                       self.temp_copy_queue, self.patchwork)
        self.llm_worker = LLMWorker(config, self.worktree_mgr, self.storage)

        # Initialize worker pool with setup and LLM workers
        self.worker_pool = WorkerPool(
            num_setup_workers=config.max_work_trees,
            num_llm_workers=config.max_claude_runs,
            setup_worker=self.setup_worker,
            llm_worker=self.llm_worker,
            review_queue=self.queue,
            temp_copy_queue=self.temp_copy_queue,
            worktree_mgr=self.worktree_mgr
        )

        # Start worker threads
        self.worker_pool.start()

        print("AIR service initialized")
        print(f"  Setup workers: {config.max_work_trees}")
        print(f"  LLM workers: {config.max_claude_runs}")
        print(f"  Max temp copies queued: {max_temp_copies}")

    def submit_review(self, data: Dict, token: str) -> str:
        """Submit a new review request

        Args:
            data: Review request data
            token: Authentication token

        Returns:
            Review ID

        Raises:
            ValueError: If request data is invalid
        """
        print(f"[submit_review] Starting submission for token: {token}")

        # Validate input - exactly one of patchwork_series_id, patches, or hash
        has_patchwork = bool('patchwork_series_id' in data and data['patchwork_series_id'])
        has_patches = bool('patches' in data and data['patches'])
        has_hash = bool('hash' in data and data['hash'])

        print(f"[submit_review] has_patchwork={has_patchwork}, has_patches={has_patches}, has_hash={has_hash}")

        input_count = sum([has_patchwork, has_patches, has_hash])
        if input_count != 1:
            print(f"[submit_review] Validation failed: input_count={input_count}")
            raise ValueError("Exactly one of patchwork_series_id, patches, or hash must be provided")

        # Validate required fields
        if 'tree' not in data or not data['tree']:
            print("[submit_review] Validation failed: tree missing")
            raise ValueError("tree is required")

        print(f"[submit_review] Creating review entry for tree: {data['tree']}")
        # Create review entry
        review_id = self.storage.create_review(token, data)
        print(f"[submit_review] Created review: {review_id}")

        # Prepare request for queue
        request = {
            'review_id': review_id,
            'token': token,
            'tree': data['tree'],
            'branch': data.get('branch'),
            'mask': data.get('mask', []),
        }

        if has_patchwork:
            request['patchwork_series_id'] = data['patchwork_series_id']
        elif has_patches:
            request['patches'] = data['patches']
        elif has_hash:
            request['hash'] = data['hash']

        # Estimate patch count for queue position calculation
        if has_patchwork and self.patchwork:
            try:
                series = self.patchwork.get('series', data['patchwork_series_id'])
                request['patch_count'] = len(series.get('patches', []))
            except Exception:
                request['patch_count'] = 1
        elif has_patches:
            request['patch_count'] = len(data['patches'])
        elif has_hash:
            # For hash/range, we'll update this after processing
            request['patch_count'] = 1

        # Add to queue
        print(f"[submit_review] Adding to queue: {review_id}")
        self.queue.put(request)

        print(f"Submitted review {review_id} to queue")
        print(f"[submit_review] Successfully submitted: {review_id}")
        return review_id

    def get_review(self, review_id: str, token: str, fmt: Optional[str] = None) -> Optional[Dict]:
        """Get review status and results

        Args:
            review_id: Review ID
            token: Authentication token
            fmt: Optional format (json, markup, inline)

        Returns:
            Review result dictionary or None if not found/unauthorized
        """
        # Get metadata
        metadata = self.storage.get_review_metadata(review_id)
        if metadata is None:
            return None

        # Check authorization (token must match or be superuser)
        if metadata['token'] != token:
            # We don't have superuser check here - would need to pass TokenAuth
            # For now, just deny access
            return None

        # Build response
        result = {
            'review_id': review_id,
            'tree': metadata['tree'],
            'status': metadata['status'],
            'date': metadata['date'],
        }

        if metadata.get('patchwork_series_id'):
            result['patchwork_series_id'] = metadata['patchwork_series_id']

        if metadata.get('hash'):
            result['hash'] = metadata['hash']

        if metadata.get('branch'):
            result['branch'] = metadata['branch']

        if metadata.get('start'):
            result['start'] = metadata['start']

        if metadata.get('start-llm'):
            result['start-llm'] = metadata['start-llm']

        if metadata.get('end'):
            result['end'] = metadata['end']

        # Add message if exists
        message = self.storage.read_message(metadata['token'], review_id)
        if message or metadata.get('message'):
            result['message'] = message or metadata.get('message')

        # Add patch counts for progress tracking
        if metadata.get('patch_count'):
            result['patch_count'] = metadata['patch_count']
        if metadata.get('completed_patches'):
            result['completed_patches'] = metadata['completed_patches']

        # Add queue position if queued
        if metadata['status'] == 'queued':
            queue_len = self.queue.get_patch_count_ahead(review_id)
            result['queue-len'] = queue_len

        # Add review results if format specified and status is done or error
        if fmt and metadata['status'] in ('done', 'error'):
            patch_count = metadata.get('patch_count', 0)
            reviews = []

            for i in range(1, patch_count + 1):
                review_content = self.storage.read_review_file(metadata['token'],
                                                              review_id, i, fmt)
                reviews.append(review_content)

            result['review'] = reviews

        return result

    def list_reviews(self, token: str, limit: int = 50, superuser: bool = False) -> List[Dict]:
        """List recent reviews for a token

        Args:
            token: Authentication token
            limit: Maximum number of reviews to return
            superuser: If True, return all reviews regardless of token

        Returns:
            List of review summaries
        """
        reviews = self.storage.list_reviews(token, limit, superuser=superuser)

        # Return simplified view
        return [{
            'review_id': r['id'],
            'status': r['status'],
            'date': r['date'],
            'tree': r['tree'],
            'patch_count': r.get('patch_count', 0)
        } for r in reviews]

    def get_status(self) -> Dict:
        """Get service status

        Returns:
            Service status dictionary
        """
        # Reload metadata to get current counts
        # (workers may have modified it)
        with self.storage.lock:
            self.storage.load_metadata()
            all_reviews = list(self.storage.reviews.values())

        status_counts = {
            'queued': sum(1 for r in all_reviews if r['status'] == 'queued'),
            'in-progress': sum(1 for r in all_reviews if r['status'] == 'in-progress'),
            'done': sum(1 for r in all_reviews if r['status'] == 'done'),
            'error': sum(1 for r in all_reviews if r['status'] == 'error'),
        }

        return {
            'service': 'air',
            'status': 'running',
            'queue_size': self.queue.size(),
            'max_work_trees': self.config.max_work_trees,
            'max_claude_runs': self.config.max_claude_runs,
            'review_counts': status_counts,
        }
