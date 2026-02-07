# SPDX-License-Identifier: GPL-2.0

"""Main AIR service orchestrator"""

import os
from datetime import datetime, timedelta
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

    def __init__(self, config, token_auth=None):
        """Initialize AIR service

        Args:
            config: AirConfig instance
            token_auth: TokenAuth instance (optional, needed for public_read support)
        """
        self.config = config
        self.token_auth = token_auth

        # Initialize components
        self.storage = ReviewStorage(config.results_path)
        self.queue = ReviewQueue(os.path.join(config.results_path, 'queue.json'))
        self.worktree_mgr = WorkTreeManager(config.git_tree, config.temp_copies_path, config.max_work_trees)

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

        # Initialize LLM worker (setup workers created per-thread in WorkerPool)
        self.llm_worker = LLMWorker(config, self.worktree_mgr, self.storage)

        # Initialize worker pool with setup and LLM workers
        self.worker_pool = WorkerPool(
            num_setup_workers=config.max_work_trees,
            num_llm_workers=config.max_claude_runs,
            config=config,
            storage=self.storage,
            llm_worker=self.llm_worker,
            review_queue=self.queue,
            temp_copy_queue=self.temp_copy_queue,
            worktree_mgr=self.worktree_mgr,
            patchwork=self.patchwork
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

        # Check if token is allowed to use this source type
        if self.token_auth:
            if has_patchwork:
                source = 'patchwork'
            elif has_patches:
                source = 'patches'
            else:
                source = 'hash'

            if not self.token_auth.is_source_allowed(token, source):
                print(f"[submit_review] Token not allowed to use source: {source}")
                raise ValueError(f"Token is not allowed to submit via {source}")

        # Validate required fields
        if 'tree' not in data or not data['tree']:
            print("[submit_review] Validation failed: tree missing")
            raise ValueError("tree is required")

        # Normalize model: if not specified, use config default
        if 'model' not in data or not data['model']:
            data['model'] = self.config.claude_model

        # Normalize and validate llm_mode: default to 'classic'
        llm_mode = data.get('llm_mode', 'classic')
        if not llm_mode:
            llm_mode = 'classic'
        if llm_mode not in ('classic', 'orc'):
            raise ValueError(f"Invalid llm_mode: {llm_mode}. Must be 'classic' or 'orc'")
        data['llm_mode'] = llm_mode

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

    def get_review(self, review_id: str, token: Optional[str] = None, fmt: Optional[str] = None) -> Optional[Dict]:
        """Get review status and results

        Args:
            review_id: Review ID
            token: Authentication token (optional for public_read reviews)
            fmt: Optional format (json, markup, inline, metadata)

        Returns:
            Review result dictionary or None if not found/unauthorized
        """
        # Get metadata
        metadata = self.storage.get_review_metadata(review_id)
        if metadata is None:
            return None

        # Check authorization
        # Allow access if:
        # 1. Review's token has public_read enabled (look up in token DB), OR
        # 2. Provided token matches the review's token, OR
        # 3. Provided token is a superuser (if token_auth is available)
        review_token = metadata['token']
        is_public = self.token_auth.is_public_read(review_token) if self.token_auth else False
        is_owner = token and review_token == token
        is_superuser = token and self.token_auth and self.token_auth.is_superuser(token)

        if not (is_public or is_owner or is_superuser):
            return None

        # JSON format is only available to superusers
        if fmt == 'json' and not is_superuser:
            return None

        # Delay public access to review content by 20 hours
        # Users must authenticate to access fresh public reviews
        if is_public and not (is_owner or is_superuser) and fmt:
            review_date_str = metadata.get('date')
            if review_date_str:
                review_date = datetime.fromisoformat(review_date_str)
                age = datetime.utcnow() - review_date
                if age < timedelta(hours=20):
                    hours_remaining = 20 - (age.total_seconds() / 3600)
                    raise ValueError(
                        f"Public read access available in {hours_remaining:.1f} hours."
                    )

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

        if metadata.get('model'):
            result['model'] = metadata['model']

        if metadata.get('llm_mode'):
            result['llm_mode'] = metadata['llm_mode']

        # Add message if exists
        message = self.storage.read_message(metadata['token'], review_id)
        if message or metadata.get('message'):
            result['message'] = message or metadata.get('message')

        # Add patch counts for progress tracking
        if metadata.get('patch_count'):
            result['patch_count'] = metadata['patch_count']
        if metadata.get('completed_patches'):
            result['completed_patches'] = metadata['completed_patches']
        if metadata.get('failed_patch_nums'):
            result['failed_patch_nums'] = metadata['failed_patch_nums']

        # Add cost (only for superusers)
        if is_superuser and metadata.get('cost_usd'):
            result['cost_usd'] = metadata['cost_usd']

        # Add feedback (for superusers and owners)
        if (is_superuser or is_owner) and metadata.get('feedback'):
            result['feedback'] = metadata['feedback']

        # Add queue position if queued
        if metadata['status'] == 'queued':
            queue_len = self.queue.get_patch_count_ahead(review_id)
            result['queue-len'] = queue_len

        # For owners/superusers viewing public reviews, show time until public access
        if is_public and (is_owner or is_superuser):
            review_date_str = metadata.get('date')
            if review_date_str:
                review_date = datetime.fromisoformat(review_date_str)
                age = datetime.utcnow() - review_date
                if age < timedelta(hours=20):
                    hours_remaining = 20 - (age.total_seconds() / 3600)
                    result['public_in_hours'] = round(hours_remaining, 1)

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

    def get_review_by_patchwork_id(self, patchwork_series_id: int, token: Optional[str] = None, fmt: Optional[str] = None) -> Optional[Dict]:
        """Get review by Patchwork series ID

        Args:
            patchwork_series_id: Patchwork series ID
            token: Authentication token (optional for public_read reviews)
            fmt: Optional format (json, markup, inline, metadata)

        Returns:
            Review result dictionary or None if not found/unauthorized
        """
        # Find review ID by Patchwork series ID (most recent)
        review_id = self.storage.find_review_by_patchwork_id(patchwork_series_id)
        if review_id is None:
            return None

        # Return full review data using existing method (handles authorization)
        return self.get_review(review_id, token, fmt)

    def list_reviews(self, token: Optional[str] = None, limit: int = 50,
                     filter_user: bool = True, has_feedback: Optional[bool] = None) -> List[Dict]:
        """List recent reviews

        Args:
            token: Authentication token (optional)
            limit: Maximum number of reviews to return
            filter_user: If True, return only reviews for this token.
                        If False, return as many reviews as possible based on permissions:
                          - Superuser token: all reviews
                          - Regular token: user's reviews + public reviews
                          - No token: public reviews only
            has_feedback: If True, only return reviews with feedback.
                         If False, only return reviews without feedback.
                         If None, return all reviews regardless of feedback.

        Returns:
            List of review summaries
        """
        # Check if the requesting user is a superuser (for showing costs and permissions)
        is_requesting_superuser = token and self.token_auth and self.token_auth.is_superuser(token)

        if filter_user:
            # Return only reviews for this token
            reviews = self.storage.list_reviews(token, limit, filter_user=True)
        else:
            # Return as many reviews as possible based on permissions
            # Get all reviews from storage
            reviews = self.storage.list_reviews(None, limit, filter_user=False)

            # Filter based on permissions
            if is_requesting_superuser:
                # Superuser sees all reviews
                pass
            elif token:
                # Regular user sees their own reviews + public reviews
                user_token = token
                if self.token_auth:
                    reviews = [r for r in reviews
                              if r.get('token') == user_token or
                                 self.token_auth.is_public_read(r.get('token', ''))]
                else:
                    # No token_auth - can only see own reviews
                    reviews = [r for r in reviews if r.get('token') == user_token]
            else:
                # No token - only public reviews
                if self.token_auth:
                    reviews = [r for r in reviews
                              if self.token_auth.is_public_read(r.get('token', ''))]
                else:
                    reviews = []

        # Filter by has_feedback if specified
        if has_feedback is not None:
            reviews = [r for r in reviews if r.get('has_feedback', False) == has_feedback]

        # Return simplified view with additional fields
        result = []
        for r in reviews:
            review_info = {
                'review_id': r['id'],
                'status': r['status'],
                'date': r['date'],
                'tree': r['tree'],
                'patch_count': r.get('patch_count', 0)
            }

            # Add patchwork series ID if present
            if r.get('patchwork_series_id'):
                review_info['patchwork_series_id'] = r['patchwork_series_id']

            # Add hash if present
            if r.get('hash'):
                review_info['hash'] = r['hash']

            # Add model if present
            if r.get('model'):
                review_info['model'] = r['model']

            # Add llm_mode if present
            if r.get('llm_mode'):
                review_info['llm_mode'] = r['llm_mode']

            # Add has_feedback flag
            if 'has_feedback' in r:
                review_info['has_feedback'] = r['has_feedback']

            # Add cost (only for superusers)
            if is_requesting_superuser and r.get('cost_usd'):
                review_info['cost_usd'] = r['cost_usd']

            # Add feedback (for superusers and owners)
            is_owner = token and r.get('token') == token
            if (is_requesting_superuser or is_owner) and r.get('feedback'):
                review_info['feedback'] = r['feedback']

            # Calculate duration for completed/error reviews
            if r.get('start') and r.get('end'):
                try:
                    from datetime import datetime
                    start_dt = datetime.fromisoformat(r['start'])
                    end_dt = datetime.fromisoformat(r['end'])
                    duration_seconds = (end_dt - start_dt).total_seconds()
                    review_info['duration_seconds'] = int(duration_seconds)
                except (ValueError, TypeError):
                    pass

            # Add token owner name if token_auth is available
            if self.token_auth:
                token_info = self.token_auth.get_token_info(r.get('token', ''))
                if token_info:
                    review_info['token_owner'] = token_info.get('name', 'Unknown')
                    review_info['is_public'] = token_info.get('public_read', False)
                else:
                    review_info['token_owner'] = 'Unknown'
                    review_info['is_public'] = False
            else:
                review_info['is_public'] = False

            result.append(review_info)

        return result

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

        now = datetime.utcnow()
        cutoff_24h = now - timedelta(hours=24)
        cutoff_7d = now - timedelta(days=7)
        cutoff_28d = now - timedelta(days=28)

        queue_size = sum(1 for r in all_reviews if r['status'] == 'queued')
        active_count = sum(1 for r in all_reviews if r['status'] == 'in-progress')

        # Find oldest active review start time
        oldest_active_start = None
        for r in all_reviews:
            if r['status'] == 'in-progress' and r.get('start'):
                try:
                    start_time = datetime.fromisoformat(r['start'])
                    if oldest_active_start is None or start_time < oldest_active_start:
                        oldest_active_start = start_time
                except (ValueError, TypeError):
                    pass

        # Calculate stats by time period
        completed_24h = 0
        feedback_24h = 0
        errors_24h = 0
        completed_7d = 0
        feedback_7d = 0
        errors_7d = 0
        cost_7d = 0.0
        patches_7d = 0
        completed_28d = 0
        feedback_28d = 0
        errors_28d = 0
        cost_28d = 0.0
        patches_28d = 0
        completed_total = 0
        feedback_total = 0
        errors_total = 0
        cost_total = 0.0
        patches_total = 0
        last_completed_time = None

        for r in all_reviews:
            # Count totals
            if r['status'] == 'done':
                completed_total += 1
                patches_total += r.get('patch_count', 0)
                if r.get('has_feedback'):
                    feedback_total += 1
            elif r['status'] == 'error':
                errors_total += 1

            cost_total += r.get('cost_usd', 0.0)

            # Get end time for time-based stats
            end_str = r.get('end')
            if not end_str:
                continue

            try:
                end_time = datetime.fromisoformat(end_str)
            except (ValueError, TypeError):
                continue

            # Track last completed time
            if r['status'] in ('done', 'error'):
                if last_completed_time is None or end_time > last_completed_time:
                    last_completed_time = end_time

            # Count by time period
            cost = r.get('cost_usd', 0.0)

            if end_time >= cutoff_24h:
                if r['status'] == 'done':
                    completed_24h += 1
                    if r.get('has_feedback'):
                        feedback_24h += 1
                elif r['status'] == 'error':
                    errors_24h += 1

            if end_time >= cutoff_7d:
                if r['status'] == 'done':
                    completed_7d += 1
                    patches_7d += r.get('patch_count', 0)
                    if r.get('has_feedback'):
                        feedback_7d += 1
                elif r['status'] == 'error':
                    errors_7d += 1
                cost_7d += cost

            if end_time >= cutoff_28d:
                if r['status'] == 'done':
                    completed_28d += 1
                    patches_28d += r.get('patch_count', 0)
                    if r.get('has_feedback'):
                        feedback_28d += 1
                elif r['status'] == 'error':
                    errors_28d += 1
                cost_28d += cost

        result = {
            'service': 'air',
            'status': 'running',
            'queue_size': queue_size,
            'active_count': active_count,
            'max_work_trees': self.config.max_work_trees,
            'max_claude_runs': self.config.max_claude_runs,
            'completed_24h': completed_24h,
            'feedback_24h': feedback_24h,
            'errors_24h': errors_24h,
            'stats': {
                '7d': {
                    'completed': completed_7d,
                    'patches': patches_7d,
                    'feedback': feedback_7d,
                    'errors': errors_7d,
                    'cost': round(cost_7d, 2) if cost_7d > 0 else None,
                },
                '28d': {
                    'completed': completed_28d,
                    'patches': patches_28d,
                    'feedback': feedback_28d,
                    'errors': errors_28d,
                    'cost': round(cost_28d, 2) if cost_28d > 0 else None,
                },
                'total': {
                    'completed': completed_total,
                    'patches': patches_total,
                    'feedback': feedback_total,
                    'errors': errors_total,
                    'cost': round(cost_total, 2) if cost_total > 0 else None,
                },
            },
        }

        if oldest_active_start:
            result['oldest_active_start'] = oldest_active_start.isoformat()

        if last_completed_time:
            result['last_completed'] = last_completed_time.isoformat()

        return result

    def set_feedback(self, review_id: str, feedback: str) -> bool:
        """Set feedback for a review

        Args:
            review_id: Review ID
            feedback: Feedback value (emailed, false-positive, false-negative)

        Returns:
            True if successful, False if review not found
        """
        return self.storage.set_feedback(review_id, feedback)

    def delete_review(self, review_id: str) -> bool:
        """Delete a review

        Args:
            review_id: Review ID

        Returns:
            True if successful, False if review not found
        """
        return self.storage.delete_review(review_id)
