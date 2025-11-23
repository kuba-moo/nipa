# SPDX-License-Identifier: GPL-2.0

"""Setup worker for preparing patches for review"""

import os
import subprocess
import tempfile
import time
from typing import List, Optional, Dict, Tuple

from pw import Patchwork
from core import log_init
from .log_helper import log_thread


class SetupWorker:
    """Worker for setting up patches/commits for review (git operations, semcode indexing)"""

    def __init__(self, config, worktree_mgr, storage, temp_copy_queue, patchwork: Optional[Patchwork] = None):
        """Initialize setup worker

        Args:
            config: AirConfig instance
            worktree_mgr: WorkTreeManager instance
            storage: ReviewStorage instance
            temp_copy_queue: TempCopyQueue instance
            patchwork: Patchwork instance (optional)
        """
        self.config = config
        self.worktree_mgr = worktree_mgr
        self.storage = storage
        self.temp_copy_queue = temp_copy_queue
        self.patchwork = patchwork

    def worker_loop(self, worker_id: int, wt_id: int, review_queue):
        """Main worker loop for a setup worker

        Args:
            worker_id: Worker ID number
            wt_id: Dedicated work tree ID for this worker
            review_queue: ReviewQueue to get review requests from
        """
        # Initialize logging for this thread
        log_init("stdout", "")

        log_thread(f"Setup worker {worker_id} started with work tree {wt_id}")

        while True:
            # Get next review request
            request = review_queue.get(timeout=1)
            if request is None:
                continue

            review_id = request.get('review_id')
            log_thread(f"Setup worker {worker_id} processing review {review_id}")

            try:
                self.process_review(wt_id, request)
            except Exception as e:
                log_thread(f"Error in setup worker {worker_id} processing {review_id}: {e}")
                import traceback
                traceback.print_exc()
                if review_id:
                    self.storage.update_review_status(review_id, 'error', f'Setup failed: {str(e)}')

    def process_review(self, wt_id: int, request: Dict):
        """Process a review request (setup phase only)

        Args:
            wt_id: Work tree ID to use
            request: Review request dictionary
        """
        review_id = request['review_id']
        token = request['token']

        # Update status to in-progress
        self.storage.update_review_status(review_id, 'in-progress')

        # Setup remote
        remote_name, branch = self._setup_remote(wt_id, request)
        if remote_name is None:
            self.storage.update_review_status(review_id, 'error', 'Failed to setup git remote')
            self.storage.write_message(token, review_id, 'Failed to setup git remote')
            return

        # Get commit hashes
        commit_hashes, git_range = self._get_commit_hashes(wt_id, request, remote_name, branch, token, review_id)
        if commit_hashes is None:
            return

        # Update patch count
        self.storage.set_patch_count(review_id, len(commit_hashes))

        # Run semcode indexing
        if not self.config.skip_semcode:
            if not self._run_semcode_index(wt_id, git_range):
                self.storage.update_review_status(review_id, 'error', 'Failed to run semcode indexing')
                self.storage.write_message(token, review_id, 'Failed to run semcode indexing')
                return
        else:
            log_thread("Skipping semcode-index (--dev-skip-semcode enabled)")

        # Create temp copies and queue them for LLM workers
        mask = request.get('mask', [])
        for i, commit_hash in enumerate(commit_hashes, 1):
            # Check mask
            if i - 1 < len(mask) and not mask[i - 1]:
                # Skip this patch (masked) - mark as complete
                log_thread(f"Skipping masked patch {i} for review {review_id}")
                self.storage.mark_patch_complete(review_id, i, success=True)
                continue

            # Create temp copy
            temp_path = self.worktree_mgr.create_temp_copy(wt_id, commit_hash)

            # Reset to commit
            if not self.worktree_mgr.git_reset_hard(temp_path, commit_hash):
                log_thread(f"Failed to reset temp copy to {commit_hash}")
                # Clean up this temp copy
                self.worktree_mgr.remove_temp_copy(temp_path)
                continue

            # Queue for LLM worker
            temp_copy_info = {
                'temp_path': temp_path,
                'token': token,
                'review_id': review_id,
                'patch_num': i,
                'commit_hash': commit_hash,
            }

            # Block if queue is full (this prevents setup workers from getting too far ahead)
            self.temp_copy_queue.put(temp_copy_info, block=True)
            time.sleep(0.1)   # Give the worker a chance to start, otherwise prints get mangled
            log_thread(f"Queued temp copy for review {review_id} patch {i}")

        log_thread(f"Setup complete for review {review_id}, queued {len(commit_hashes)} temp copies")

    def _setup_remote(self, wt_id: int, request: Dict) -> Tuple[Optional[str], Optional[str]]:
        """Setup git remote for the review

        Args:
            wt_id: Work tree ID
            request: Review request

        Returns:
            Tuple of (remote_name, branch) or (None, None) on error
        """
        tree_name = request['tree']
        branch = request.get('branch')

        # Check if remote already exists
        remote_name = tree_name

        # Try to add remote if it doesn't exist
        remote_url = f"git://git.kernel.org/pub/scm/linux/kernel/git/{tree_name}.git"
        if not self.worktree_mgr.add_remote(remote_name, remote_url):
            log_thread(f"Failed to add remote {remote_name}")
            return None, None

        # Fetch the remote
        if not self.worktree_mgr.git_fetch(wt_id, remote_name):
            log_thread(f"Failed to fetch remote {remote_name}")
            return None, None

        # Determine branch
        if branch is None:
            branch = self.worktree_mgr.get_default_branch(wt_id, remote_name)
            if branch is None:
                log_thread(f"Failed to determine default branch for {remote_name}")
                return None, None

        return remote_name, branch

    def _get_commit_hashes(self, wt_id: int, request: Dict, remote_name: str,
                          branch: str, token: str, review_id: str) -> Tuple[Optional[List[str]], Optional[str]]:
        """Get list of commit hashes to review

        Args:
            wt_id: Work tree ID
            request: Review request
            remote_name: Git remote name
            branch: Git branch name
            token: Auth token
            review_id: Review ID

        Returns:
            Tuple of (commit_hashes list, git_range) or (None, None) on error
        """
        wt_path = self.worktree_mgr.get_work_tree_path(wt_id)

        # Case 1: Hash or hash range provided
        if 'hash' in request and request['hash']:
            hash_str = request['hash']

            # Check if it's a range
            if '..' in hash_str:
                git_range = hash_str
            else:
                # Single hash - convert to range
                git_range = f"{hash_str}^..{hash_str}"

            # Verify commits exist
            for h in [hash_str.split('..')[0] if '..' in hash_str else hash_str]:
                if not self.worktree_mgr.check_commit_exists(wt_id, h):
                    self.storage.update_review_status(review_id, 'error', f'Commit {h} not found')
                    self.storage.write_message(token, review_id, f'Commit {h} not found')
                    return None, None

            # Convert range to list of hashes
            commit_hashes = self._range_to_hashes(wt_path, git_range)
            return commit_hashes, git_range

        # Case 2: Patches provided (raw or patchwork)
        if not self.worktree_mgr.git_reset_hard(wt_path, f"{remote_name}/{branch}"):
            self.storage.update_review_status(review_id, 'error', f'Failed to reset to {remote_name}/{branch}')
            self.storage.write_message(token, review_id, f'Failed to reset to {remote_name}/{branch}')
            return None, None

        # Get patches
        patches = []
        if 'patchwork_series_id' in request and request['patchwork_series_id']:
            if self.patchwork is None:
                self.storage.update_review_status(review_id, 'error', 'Patchwork not configured')
                self.storage.write_message(token, review_id, 'Patchwork not configured')
                return None, None

            # Fetch patches from patchwork
            series_id = request['patchwork_series_id']
            try:
                mbox = self.patchwork.get_mbox('series', series_id)
                patches = [mbox]  # mbox contains all patches
            except Exception as e:
                log_thread(f"Failed to fetch patchwork series {series_id}: {e}")
                self.storage.update_review_status(review_id, 'error',
                                                 f'Failed to fetch patchwork series {series_id}')
                self.storage.write_message(token, review_id,
                                          f'Failed to fetch patchwork series {series_id}: {e}')
                return None, None
        elif 'patches' in request and request['patches']:
            patches = request['patches']
        else:
            self.storage.update_review_status(review_id, 'error', 'No patches or hashes provided')
            self.storage.write_message(token, review_id, 'No patches or hashes provided')
            return None, None

        # Apply patches and get commit hashes
        base_ref = f"{remote_name}/{branch}"
        commit_hashes = self._apply_patches(wt_path, patches, token, review_id)
        if commit_hashes is None:
            return None, None

        git_range = f"{base_ref}..HEAD"
        return commit_hashes, git_range

    def _apply_patches(self, wt_path: str, patches: List[str],
                      token: str, review_id: str) -> Optional[List[str]]:
        """Apply patches and return commit hashes

        Args:
            wt_path: Work tree path
            patches: List of patch contents (for mbox files, each may contain multiple patches)
            token: Auth token
            review_id: Review ID

        Returns:
            List of commit hashes or None on error
        """
        commit_hashes = []

        for i, patch_content in enumerate(patches, 1):
            # Write patch to temp file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.patch', delete=False) as f:
                f.write(patch_content)
                patch_file = f.name

            try:
                # Get current HEAD before applying patches
                result = subprocess.run(['git', 'rev-parse', 'HEAD'],
                                      cwd=wt_path, capture_output=True, text=True, check=True)
                head_before = result.stdout.strip()

                # Apply patch (may contain multiple patches if it's an mbox)
                result = subprocess.run(['git', 'am', patch_file],
                                      cwd=wt_path, capture_output=True, text=True)
                if result.returncode != 0:
                    log_thread(f"Failed to apply patch {i}: {result.stderr}")
                    self.storage.update_review_status(review_id, 'error',
                                                     f'Failed to apply patch {i}')
                    self.storage.write_message(token, review_id,
                                              f'Failed to apply patch {i}: {result.stderr}')
                    return None

                # Get all commits created by this git am (in case mbox contains multiple patches)
                result = subprocess.run(['git', 'rev-list', f'{head_before}..HEAD'],
                                      cwd=wt_path, capture_output=True, text=True, check=True)
                new_commits = result.stdout.strip().split('\n')
                # rev-list returns newest first, we want oldest first
                new_commits = list(reversed([c for c in new_commits if c]))

                # Store patch content for each commit
                start_patch_num = len(commit_hashes) + 1
                for j, commit_hash in enumerate(new_commits):
                    patch_num = start_patch_num + j
                    self.storage.write_patch_file(token, review_id, patch_num, patch_content)

                # Add all new commits
                commit_hashes.extend(new_commits)

            finally:
                os.unlink(patch_file)

        return commit_hashes

    def _range_to_hashes(self, wt_path: str, git_range: str) -> List[str]:
        """Convert git range to list of commit hashes

        Args:
            wt_path: Work tree path
            git_range: Git range (e.g., hash1..hash2)

        Returns:
            List of commit hashes
        """
        result = subprocess.run(['git', 'rev-list', git_range],
                              cwd=wt_path, capture_output=True, text=True, check=True)
        hashes = result.stdout.strip().split('\n')
        # rev-list returns newest first, we want oldest first
        return list(reversed(hashes))

    def _run_semcode_index(self, wt_id: int, git_range: str) -> bool:
        """Run semcode indexing

        Args:
            wt_id: Work tree ID
            git_range: Git range to index

        Returns:
            True if successful, False otherwise
        """
        wt_path = self.worktree_mgr.get_work_tree_path(wt_id)

        log_thread(f"Running semcode-index for range {git_range}")
        try:
            result = subprocess.run(['semcode-index', '-s', '.', '--git', git_range],
                                  cwd=wt_path, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                log_thread(f"semcode-index failed: {result.stderr}")
                return False
            return True
        except subprocess.TimeoutExpired:
            log_thread("semcode-index timed out")
            return False
        except Exception as e:
            log_thread(f"semcode-index error: {e}")
            return False
