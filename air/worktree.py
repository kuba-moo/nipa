# SPDX-License-Identifier: GPL-2.0

"""Work tree management for AIR service"""

import os
import shutil
import subprocess
from typing import Optional
from threading import Lock

from .log_helper import log_thread


class WorkTreeManager:
    """Manages git work trees for parallel review processing"""

    def __init__(self, git_tree: str, max_work_trees: int):
        """Initialize work tree manager

        Args:
            git_tree: Path to the main git repository
            max_work_trees: Maximum number of work trees to create
        """
        self.git_tree = git_tree
        self.max_work_trees = max_work_trees
        self.work_trees = {}  # work_tree_id -> {'path': path, 'in_use': bool}
        self.lock = Lock()

        # Initialize work trees
        self._init_work_trees()

    def _init_work_trees(self):
        """Initialize work tree directories using git worktree"""
        for i in range(1, self.max_work_trees + 1):
            wt_name = f"wt-{i}"
            wt_path = os.path.join(self.git_tree, wt_name)

            # Check if work tree already exists
            if not os.path.exists(wt_path):
                log_thread(f"Creating work tree {wt_name} at {wt_path}")
                try:
                    # Create git worktree - this shares .git with main repo
                    # Use detached HEAD so we can switch to any branch/commit
                    subprocess.run(['git', 'worktree', 'add', '--detach', wt_name],
                                 cwd=self.git_tree, check=True, capture_output=True)
                except subprocess.CalledProcessError as e:
                    log_thread(f"Error creating work tree {wt_name}: {e}")
                    raise

            self.work_trees[i] = {
                'path': wt_path,
                'in_use': False
            }

    def acquire_work_tree(self) -> Optional[int]:
        """Acquire an available work tree

        Returns:
            Work tree ID if available, None otherwise
        """
        with self.lock:
            for wt_id, wt_info in self.work_trees.items():
                if not wt_info['in_use']:
                    wt_info['in_use'] = True
                    return wt_id
            return None

    def release_work_tree(self, wt_id: int):
        """Release a work tree

        Args:
            wt_id: Work tree ID to release
        """
        with self.lock:
            if wt_id in self.work_trees:
                self.work_trees[wt_id]['in_use'] = False

    def get_work_tree_path(self, wt_id: int) -> Optional[str]:
        """Get the path to a work tree

        Args:
            wt_id: Work tree ID

        Returns:
            Path to work tree or None
        """
        if wt_id in self.work_trees:
            return self.work_trees[wt_id]['path']
        return None

    def create_temp_copy(self, wt_id: int, commit_hash: str) -> str:
        """Create a temporary copy of work tree for reviewing a specific commit

        Args:
            wt_id: Work tree ID
            commit_hash: Commit hash to review

        Returns:
            Path to temporary work tree copy
        """
        wt_path = self.get_work_tree_path(wt_id)
        if not wt_path:
            raise ValueError(f"Work tree {wt_id} not found")

        # Create temp copy with commit hash in name
        temp_path = f"{wt_path}.{commit_hash[:12]}"

        log_thread(f"Creating temp work tree copy: {temp_path}")
        try:
            subprocess.run(['cp', '-a', '--reflink', wt_path, temp_path],
                         check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            log_thread(f"Error creating temp work tree: {e}")
            raise

        return temp_path

    def remove_temp_copy(self, temp_path: str):
        """Remove a temporary work tree copy

        Args:
            temp_path: Path to temporary work tree
        """
        if os.path.exists(temp_path):
            log_thread(f"Removing temp work tree: {temp_path}")
            try:
                shutil.rmtree(temp_path)
            except Exception as e:
                log_thread(f"Error removing temp work tree {temp_path}: {e}")

    def git_fetch(self, wt_id: int, remote: str) -> bool:
        """Fetch from a remote in a work tree

        Args:
            wt_id: Work tree ID
            remote: Remote name

        Returns:
            True if successful, False otherwise
        """
        wt_path = self.get_work_tree_path(wt_id)
        if not wt_path:
            return False

        try:
            subprocess.run(['git', 'fetch', remote],
                         cwd=wt_path, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as e:
            log_thread(f"Error fetching remote {remote}: {e}")
            log_thread(f"stdout: {e.stdout}")
            log_thread(f"stderr: {e.stderr}")
            return False

    def git_reset_hard(self, path: str, ref: str) -> bool:
        """Reset a work tree to a specific ref

        Args:
            path: Path to work tree (can be main or temp)
            ref: Git reference (branch, tag, or commit hash)

        Returns:
            True if successful, False otherwise
        """
        try:
            subprocess.run(['git', 'reset', '--hard', ref],
                         cwd=path, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as e:
            log_thread(f"Error resetting to {ref}: {e}")
            log_thread(f"stdout: {e.stdout}")
            log_thread(f"stderr: {e.stderr}")
            return False

    def add_remote(self, remote_name: str, remote_url: str) -> bool:
        """Add a remote to the main repository (with locking)

        Args:
            remote_name: Name of the remote
            remote_url: URL of the remote

        Returns:
            True if successful or already exists, False on error
        """
        with self.lock:
            # Check if remote already exists
            try:
                result = subprocess.run(['git', 'remote', 'get-url', remote_name],
                                      cwd=self.git_tree, capture_output=True, check=False)
                if result.returncode == 0:
                    log_thread(f"Remote {remote_name} already exists")
                    return True
            except Exception:
                pass

            # Add the remote
            try:
                subprocess.run(['git', 'remote', 'add', remote_name, remote_url],
                             cwd=self.git_tree, check=True, capture_output=True)
                log_thread(f"Added remote {remote_name}: {remote_url}")
                return True
            except subprocess.CalledProcessError as e:
                log_thread(f"Error adding remote {remote_name}: {e}")
                return False

    def check_commit_exists(self, wt_id: int, commit_hash: str) -> bool:
        """Check if a commit exists in a work tree

        Args:
            wt_id: Work tree ID
            commit_hash: Commit hash to check

        Returns:
            True if commit exists, False otherwise
        """
        wt_path = self.get_work_tree_path(wt_id)
        if not wt_path:
            return False

        try:
            subprocess.run(['git', 'cat-file', '-e', commit_hash],
                         cwd=wt_path, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def get_default_branch(self, wt_id: int, remote: str) -> Optional[str]:
        """Get the default branch of a remote

        Args:
            wt_id: Work tree ID
            remote: Remote name

        Returns:
            Default branch name (e.g., 'main', 'master') or None
        """
        wt_path = self.get_work_tree_path(wt_id)
        if not wt_path:
            return None

        try:
            result = subprocess.run(['git', 'symbolic-ref', f'refs/remotes/{remote}/HEAD'],
                                  cwd=wt_path, capture_output=True, text=True, check=True)
            # Output is like 'refs/remotes/origin/main'
            ref = result.stdout.strip()
            return ref.split('/')[-1]
        except subprocess.CalledProcessError:
            # Try alternate method
            try:
                result = subprocess.run(['git', 'remote', 'show', remote],
                                      cwd=wt_path, capture_output=True, text=True, check=True)
                for line in result.stdout.split('\n'):
                    if 'HEAD branch:' in line:
                        return line.split(':')[1].strip()
            except subprocess.CalledProcessError:
                pass

        return None
