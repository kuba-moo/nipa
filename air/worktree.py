# SPDX-License-Identifier: GPL-2.0

"""Repository management for AIR service"""

import os
import shutil
import subprocess
from typing import Optional
from threading import Lock

from .log_helper import log_thread


class WorkTreeManager:
    """Manages git repository for review processing

    This class works directly with the main git repository and creates
    temporary copies for individual patch reviews.
    """

    def __init__(self, git_tree: str, max_work_trees: int):
        """Initialize repository manager

        Args:
            git_tree: Path to the main git repository
            max_work_trees: Must be 1 (multiple work trees not implemented)
        """
        if max_work_trees != 1:
            raise NotImplementedError(
                f"max_work_trees={max_work_trees} is not supported. "
                "Only max_work_trees=1 is implemented."
            )

        self.git_tree = git_tree
        self.max_work_trees = max_work_trees
        self.lock = Lock()

    def get_work_tree_path(self, wt_id: int) -> Optional[str]:
        """Get the path to the main repository

        Args:
            wt_id: Work tree ID (must be 1)

        Returns:
            Path to main repository or None if invalid ID
        """
        if wt_id != 1:
            return None
        return self.git_tree

    def get_git_dir(self, wt_id: int) -> Optional[str]:
        """Get the path to the git metadata directory

        Args:
            wt_id: Work tree ID (must be 1)

        Returns:
            Path to .git directory or None if invalid ID
        """
        if wt_id != 1:
            return None
        return os.path.join(self.git_tree, '.git')

    def create_temp_copy(self, wt_id: int, commit_hash: str) -> str:
        """Create a temporary copy of the repo for reviewing a specific commit

        Args:
            wt_id: Work tree ID (must be 1)
            commit_hash: Commit hash to review

        Returns:
            Path to temporary repo copy
        """
        if wt_id != 1:
            raise ValueError(f"Invalid work tree ID: {wt_id}")

        # Create temp copy with wt- prefix and commit hash in name
        temp_name = f"wt-{commit_hash[:12]}"
        temp_path = os.path.join(self.git_tree, temp_name)

        # To avoid recursive copy (copying into itself), we first copy to a
        # sibling directory, then move it inside. This preserves reflinks
        # since the move is just a rename on the same filesystem.
        parent_dir = os.path.dirname(self.git_tree)
        staging_path = os.path.join(parent_dir, temp_name)

        log_thread(f"Creating temp repo copy: {temp_path}")
        try:
            # Step 1: Copy to sibling location (avoids recursion)
            result = subprocess.run([
                'cp', '-a', '--reflink=auto',
                self.git_tree,
                staging_path
            ], check=True, capture_output=True, text=True)

            # Step 2: Move into the git tree (just a rename, preserves reflinks)
            shutil.move(staging_path, temp_path)
        except subprocess.CalledProcessError as e:
            log_thread(f"Error creating temp repo copy: {e}")
            log_thread(f"stdout: {e.stdout}")
            log_thread(f"stderr: {e.stderr}")
            # Clean up staging path if it exists
            if os.path.exists(staging_path):
                shutil.rmtree(staging_path)
            raise
        except Exception as e:
            log_thread(f"Error moving temp repo copy: {e}")
            # Clean up both paths if they exist
            if os.path.exists(staging_path):
                shutil.rmtree(staging_path)
            if os.path.exists(temp_path):
                shutil.rmtree(temp_path)
            raise

        return temp_path

    def remove_temp_copy(self, temp_path: str):
        """Remove a temporary repo copy

        Args:
            temp_path: Path to temporary repo copy
        """
        if os.path.exists(temp_path):
            log_thread(f"Removing temp repo copy: {temp_path}")
            try:
                shutil.rmtree(temp_path)
            except Exception as e:
                log_thread(f"Error removing temp repo copy {temp_path}: {e}")

    def git_fetch(self, wt_id: int, remote: str) -> bool:
        """Fetch from a remote in the main repository

        Args:
            wt_id: Work tree ID (must be 1)
            remote: Remote name

        Returns:
            True if successful, False otherwise
        """
        if wt_id != 1:
            return False

        try:
            subprocess.run(['git', 'fetch', remote],
                         cwd=self.git_tree, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as e:
            log_thread(f"Error fetching remote {remote}: {e}")
            log_thread(f"stdout: {e.stdout}")
            log_thread(f"stderr: {e.stderr}")
            return False

    def git_reset_hard(self, path: str, ref: str) -> bool:
        """Reset a repo to a specific ref

        Args:
            path: Path to repo (main or temp copy)
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
        """Check if a commit exists in the repository

        Args:
            wt_id: Work tree ID (must be 1)
            commit_hash: Commit hash to check

        Returns:
            True if commit exists, False otherwise
        """
        if wt_id != 1:
            return False

        try:
            subprocess.run(['git', 'cat-file', '-e', commit_hash],
                         cwd=self.git_tree, check=True, capture_output=True)
            return True
        except subprocess.CalledProcessError:
            return False

    def get_default_branch(self, wt_id: int, remote: str) -> Optional[str]:
        """Get the default branch of a remote

        Args:
            wt_id: Work tree ID (must be 1)
            remote: Remote name

        Returns:
            Default branch name (e.g., 'main', 'master') or None
        """
        if wt_id != 1:
            return None

        try:
            result = subprocess.run(['git', 'symbolic-ref', f'refs/remotes/{remote}/HEAD'],
                                  cwd=self.git_tree, capture_output=True, text=True, check=True)
            # Output is like 'refs/remotes/origin/main'
            ref = result.stdout.strip()
            return ref.split('/')[-1]
        except subprocess.CalledProcessError:
            # Try alternate method
            try:
                result = subprocess.run(['git', 'remote', 'show', remote],
                                      cwd=self.git_tree, capture_output=True, text=True, check=True)
                for line in result.stdout.split('\n'):
                    if 'HEAD branch:' in line:
                        return line.split(':')[1].strip()
            except subprocess.CalledProcessError:
                pass

        return None
