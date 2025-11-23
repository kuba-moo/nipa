# SPDX-License-Identifier: GPL-2.0

"""Storage management for review results and metadata"""

import json
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from threading import Lock


class ReviewStorage:
    """Manages storage of review results and metadata"""

    def __init__(self, results_path: str):
        """Initialize storage manager

        Args:
            results_path: Base path for storing results
        """
        self.results_path = results_path
        self.metadata_path = os.path.join(results_path, 'metadata.json')
        self.queue_path = os.path.join(results_path, 'queue.json')
        self.lock = Lock()

        # In-memory metadata
        self.reviews: Dict[str, Dict] = {}

        # Load existing metadata
        self.load_metadata()

    def load_metadata(self):
        """Load metadata from disk"""
        try:
            with open(self.metadata_path, 'r') as f:
                self.reviews = json.load(f)
        except FileNotFoundError:
            self.reviews = {}

    def save_metadata(self):
        """Save metadata to disk (assumes lock is already held)"""
        with open(self.metadata_path, 'w') as f:
            json.dump(self.reviews, f, indent=2)

    def create_review(self, token: str, request_data: Dict) -> str:
        """Create a new review entry

        Args:
            token: Authentication token
            request_data: Review request data

        Returns:
            Review ID (UUID)
        """
        print(f"[storage.create_review] Starting for token: {token}")
        review_id = str(uuid.uuid4())
        print(f"[storage.create_review] Generated UUID: {review_id}")

        with self.lock:
            # Reload metadata to ensure we have the latest state
            self.load_metadata()
            print("[storage.create_review] Acquired lock, creating metadata entry")
            self.reviews[review_id] = {
                'id': review_id,
                'token': token,
                'status': 'queued',
                'date': datetime.utcnow().isoformat(),
                'start': None,
                'start-llm': None,
                'end': None,
                'patchwork_series_id': request_data.get('patchwork_series_id'),
                'hash': request_data.get('hash'),
                'tree': request_data.get('tree'),
                'branch': request_data.get('branch'),
                'message': None,
                'patch_count': 0,  # Will be updated when patches are processed
            }
            print("[storage.create_review] Metadata created, saving to disk")
            self.save_metadata()
            print("[storage.create_review] Metadata saved")

        # Create review directory
        print("[storage.create_review] Creating review directory")
        review_dir = self.get_review_dir(token, review_id)
        print(f"[storage.create_review] Review dir path: {review_dir}")
        os.makedirs(review_dir, exist_ok=True)
        print(f"[storage.create_review] Directory created, returning {review_id}")

        return review_id

    def get_review_dir(self, token: str, review_id: str) -> str:
        """Get directory path for a review

        Args:
            token: Authentication token
            review_id: Review ID

        Returns:
            Path to review directory
        """
        return os.path.join(self.results_path, token, review_id)

    def get_patch_dir(self, token: str, review_id: str, patch_num: int) -> str:
        """Get directory path for a specific patch in a review

        Args:
            token: Authentication token
            review_id: Review ID
            patch_num: Patch number (1-based)

        Returns:
            Path to patch directory
        """
        review_dir = self.get_review_dir(token, review_id)
        return os.path.join(review_dir, str(patch_num))

    def update_review_status(self, review_id: str, status: str, message: Optional[str] = None):
        """Update review status

        Args:
            review_id: Review ID
            status: New status (queued, in-progress, done, error)
            message: Optional message (for errors or warnings)
        """
        with self.lock:
            # Reload metadata to ensure we have the latest state
            self.load_metadata()
            if review_id not in self.reviews:
                return

            self.reviews[review_id]['status'] = status
            if status == 'in-progress' and self.reviews[review_id]['start'] is None:
                self.reviews[review_id]['start'] = datetime.utcnow().isoformat()
            if status in ('done', 'error') and self.reviews[review_id].get('end') is None:
                self.reviews[review_id]['end'] = datetime.utcnow().isoformat()
            if message:
                self.reviews[review_id]['message'] = message

            self.save_metadata()

    def set_patch_count(self, review_id: str, count: int):
        """Set the number of patches in a review

        Args:
            review_id: Review ID
            count: Number of patches
        """
        with self.lock:
            # Reload metadata to ensure we have the latest state
            self.load_metadata()
            if review_id in self.reviews:
                self.reviews[review_id]['patch_count'] = count
                self.save_metadata()

    def set_llm_start_time(self, review_id: str):
        """Set the LLM start timestamp for a review

        Args:
            review_id: Review ID
        """
        with self.lock:
            # Reload metadata to ensure we have the latest state
            self.load_metadata()
            if review_id in self.reviews:
                # Only set if not already set (for the first LLM call)
                if self.reviews[review_id].get('start-llm') is None:
                    self.reviews[review_id]['start-llm'] = datetime.utcnow().isoformat()
                    self.save_metadata()

    def mark_patch_complete(self, review_id: str, patch_num: int, success: bool):
        """Mark a patch as complete and update review status if all patches are done

        Args:
            review_id: Review ID
            patch_num: Patch number (1-based)
            success: Whether the patch review succeeded
        """
        with self.lock:
            # Reload metadata to ensure we have the latest state
            self.load_metadata()

            if review_id not in self.reviews:
                return

            # Initialize completed patches tracking if not exists
            if 'completed_patches' not in self.reviews[review_id]:
                self.reviews[review_id]['completed_patches'] = 0
            if 'failed_patches' not in self.reviews[review_id]:
                self.reviews[review_id]['failed_patches'] = 0

            self.reviews[review_id]['completed_patches'] += 1
            if not success:
                self.reviews[review_id]['failed_patches'] += 1

            completed = self.reviews[review_id]['completed_patches']
            failed = self.reviews[review_id]['failed_patches']
            total = self.reviews[review_id].get('patch_count', 0)

            # Check if all patches are complete
            if completed >= total and total > 0:
                # All patches done - mark review as complete or error
                if failed > 0:
                    self.reviews[review_id]['status'] = 'error'
                    if not self.reviews[review_id].get('message'):
                        self.reviews[review_id]['message'] = f'{failed} of {total} patches failed review'
                else:
                    self.reviews[review_id]['status'] = 'done'

                if self.reviews[review_id].get('end') is None:
                    self.reviews[review_id]['end'] = datetime.utcnow().isoformat()

            self.save_metadata()

    def write_message(self, token: str, review_id: str, message: str):
        """Write error/warning message to message file

        Args:
            token: Authentication token
            review_id: Review ID
            message: Message text
        """
        review_dir = self.get_review_dir(token, review_id)
        message_path = os.path.join(review_dir, 'message')
        with open(message_path, 'w') as f:
            f.write(message)

    def read_message(self, token: str, review_id: str) -> Optional[str]:
        """Read message file if it exists

        Args:
            token: Authentication token
            review_id: Review ID

        Returns:
            Message text or None
        """
        review_dir = self.get_review_dir(token, review_id)
        message_path = os.path.join(review_dir, 'message')
        try:
            with open(message_path, 'r') as f:
                return f.read()
        except FileNotFoundError:
            return None

    def get_review_metadata(self, review_id: str) -> Optional[Dict]:
        """Get review metadata

        Args:
            review_id: Review ID

        Returns:
            Review metadata dictionary or None
        """
        # Reload metadata from disk to ensure we have latest data
        # (workers may have modified it)
        with self.lock:
            self.load_metadata()
            return self.reviews.get(review_id)

    def list_reviews(self, token: str, limit: int = 50, superuser: bool = False) -> List[Dict]:
        """List recent reviews

        Args:
            token: Authentication token
            limit: Maximum number of reviews to return
            superuser: If True, return all reviews regardless of token

        Returns:
            List of review metadata dictionaries
        """
        with self.lock:
            # Reload metadata from disk to ensure we have latest data
            # (workers may have modified it)
            self.load_metadata()

            if superuser:
                reviews = list(self.reviews.values())
            else:
                reviews = [r for r in self.reviews.values() if r['token'] == token]

            # Sort by date (newest first)
            reviews.sort(key=lambda r: r['date'], reverse=True)
            return reviews[:limit]

    def write_patch_file(self, token: str, review_id: str, patch_num: int, content: str):
        """Write patch file content

        Args:
            token: Authentication token
            review_id: Review ID
            patch_num: Patch number (1-based)
            content: Patch content
        """
        patch_dir = self.get_patch_dir(token, review_id, patch_num)
        os.makedirs(patch_dir, exist_ok=True)
        patch_path = os.path.join(patch_dir, 'patch')
        with open(patch_path, 'w') as f:
            f.write(content)

    def read_review_file(self, token: str, review_id: str, patch_num: int,
                        fmt: str) -> Optional[str]:
        """Read a review file

        Args:
            token: Authentication token
            review_id: Review ID
            patch_num: Patch number (1-based)
            fmt: Format (json, markup, inline)

        Returns:
            File content or None
        """
        patch_dir = self.get_patch_dir(token, review_id, patch_num)

        # Map format to filename
        filename_map = {
            'json': 'review.json',
            'markup': 'review.md',
            'inline': 'review-inline.txt'
        }

        filename = filename_map.get(fmt)
        if not filename:
            return None

        filepath = os.path.join(patch_dir, filename)
        try:
            with open(filepath, 'r') as f:
                return f.read()
        except FileNotFoundError:
            return None
