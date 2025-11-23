# SPDX-License-Identifier: GPL-2.0

"""LLM worker for running Claude reviews"""

import os
import shutil
import subprocess
import time
from typing import Dict

from core import log_init
from .log_helper import log_thread, log_thread_debug
from .claude_json import convert_json_to_markdown


class LLMWorker:
    """Worker for running Claude/LLM reviews on prepared temp copies"""

    def __init__(self, config, worktree_mgr, storage):
        """Initialize LLM worker

        Args:
            config: AirConfig instance
            worktree_mgr: WorkTreeManager instance
            storage: ReviewStorage instance
        """
        self.config = config
        self.worktree_mgr = worktree_mgr
        self.storage = storage

    def worker_loop(self, worker_id: int, temp_copy_queue):
        """Main worker loop for an LLM worker

        Args:
            worker_id: Worker ID number
            temp_copy_queue: TempCopyQueue to get temp copies from
        """
        # Initialize logging for this thread
        log_init("stdout", "")

        log_thread(f"LLM worker {worker_id} started")

        while True:
            # Get next temp copy
            temp_copy_info = temp_copy_queue.get(timeout=1)
            if temp_copy_info is None:
                continue

            review_id = temp_copy_info['review_id']
            patch_num = temp_copy_info['patch_num']
            log_thread(f"LLM worker {worker_id} processing review {review_id} patch {patch_num}")

            try:
                self.process_temp_copy(temp_copy_info)
            except Exception as e:
                log_thread(f"Error in LLM worker {worker_id} processing {review_id} patch {patch_num}: {e}")
                import traceback
                traceback.print_exc()
            finally:
                # Always clean up the temp copy
                temp_path = temp_copy_info['temp_path']
                if not self.config.keep_temp_trees:
                    self.worktree_mgr.remove_temp_copy(temp_path)
                else:
                    log_thread(f"Keeping temp work tree: {temp_path} (--dev-keep-temp-trees)")

                # Mark task as done
                temp_copy_queue.task_done()

    def process_temp_copy(self, temp_copy_info: Dict):
        """Process a temp copy (run Claude review)

        Args:
            temp_copy_info: Dict with temp_path, token, review_id, patch_num, commit_hash
        """
        temp_path = temp_copy_info['temp_path']
        token = temp_copy_info['token']
        review_id = temp_copy_info['review_id']
        patch_num = temp_copy_info['patch_num']
        commit_hash = temp_copy_info['commit_hash']

        # Run Claude review with retries
        success = False
        for attempt in range(self.config.claude_retries):
            log_thread(f"Review attempt {attempt + 1} for commit {commit_hash[:8]}")

            success = self._run_claude_review(temp_path, token, review_id, patch_num, attempt + 1)
            if success:
                log_thread(f"Successfully reviewed patch {patch_num} for {review_id}")
                break

            log_thread(f"Review attempt {attempt + 1} failed for {commit_hash[:8]}")

        # Mark patch as complete (success or failure)
        if not success:
            log_thread(f"Review failed after {self.config.claude_retries} attempts for {commit_hash[:8]}")

        # Mark this patch as complete and check if all patches are done
        self.storage.mark_patch_complete(review_id, patch_num, success)

    def _save_partial_output(self, patch_dir: str, review_json_path: str, attempt: int):
        """Save partial review output if available

        Args:
            patch_dir: Directory for patch results
            review_json_path: Path to review.json file
            attempt: Attempt number
        """
        # Check if review.json has any content (partial output)
        if not os.path.exists(review_json_path) or os.path.getsize(review_json_path) == 0:
            return

        # Save partial output with attempt number
        partial_json_path = os.path.join(patch_dir, f'review-partial-attempt{attempt}.json')
        try:
            shutil.copy(review_json_path, partial_json_path)
            log_thread(f"Partial output saved to {partial_json_path}")
        except Exception:
            pass

        # Try to convert partial JSON to other formats (best effort)
        try:
            partial_md_path = os.path.join(patch_dir, f'review-partial-attempt{attempt}.md')
            convert_json_to_markdown(review_json_path, partial_md_path)
        except Exception:
            pass  # Ignore errors in format conversion

    def _run_claude_review(self, work_path: str, token: str, review_id: str,
                          patch_num: int, attempt: int = 1) -> bool:
        """Run Claude review on a commit

        Args:
            work_path: Path to work tree (temp copy)
            token: Auth token
            review_id: Review ID
            patch_num: Patch number (1-based)
            attempt: Attempt number (1-based, default 1)

        Returns:
            True if successful, False otherwise
        """
        patch_dir = self.storage.get_patch_dir(token, review_id, patch_num)

        # Create patch directory if it doesn't exist
        os.makedirs(patch_dir, exist_ok=True)

        # Copy the entire review prompt directory to the work tree
        # Strip trailing slash to ensure basename works correctly
        prompt_dir = self.config.review_prompt_dir.rstrip('/')
        prompt_dir_basename = os.path.basename(prompt_dir)
        work_prompt_dir = os.path.join(work_path, prompt_dir_basename)

        log_thread(f"Copying prompt directory to {work_prompt_dir}")

        # Remove if it already exists (from a previous attempt)
        if os.path.exists(work_prompt_dir):
            log_thread(f"  Removing existing: {work_prompt_dir}")
            shutil.rmtree(work_prompt_dir)

        # Copy the entire directory
        shutil.copytree(prompt_dir, work_prompt_dir)

        # Construct the path to the prompt file relative to work_path
        prompt_path = os.path.join(prompt_dir_basename, self.config.review_prompt_file)

        # Verify the prompt file exists
        full_prompt_path = os.path.join(work_path, prompt_path)
        if not os.path.exists(full_prompt_path):
            log_thread_debug("WARNING", f"  Prompt NOT found: {full_prompt_path}")

        # Build Claude command
        cmd = [
            'claude',
            '--mcp-config', self.config.mcp_config,
            '--strict-mcp-config',
            '--allowedTools', self.config.mcp_tools,
            '--model', self.config.claude_model,
            '-p', f'review the top commit in this directory using prompt {full_prompt_path}',
            '--verbose',
            '--output-format=stream-json'
        ]

        log_thread(f"Claude cwd: {work_path} prompt {full_prompt_path}")

        review_json_path = os.path.join(patch_dir, 'review.json')
        review_md_path = os.path.join(patch_dir, 'review.md')

        try:
            # Record LLM start time (only on first attempt of first patch)
            if attempt == 1 and patch_num == 1:
                self.storage.set_llm_start_time(review_id)

            # Run Claude and capture output
            start_time = time.time()
            with open(review_json_path, 'w') as json_file:
                result = subprocess.run(cmd, cwd=work_path, stdout=json_file,
                                      stderr=subprocess.PIPE, timeout=self.config.claude_timeout)
            elapsed = time.time() - start_time

            if result.returncode != 0:
                log_thread(f"Claude review failed for {review_id} patch {patch_num} after {elapsed:.1f}s: {result.stderr.decode()}")
                # Save stderr for inspection
                stderr_path = os.path.join(patch_dir, f'claude-stderr-attempt{attempt}.txt')
                with open(stderr_path, 'w') as f:
                    f.write(result.stderr.decode())

                # Save any partial output
                self._save_partial_output(patch_dir, review_json_path, attempt)
                return False

            log_thread(f"Claude review completed for {review_id} patch {patch_num} in {elapsed:.1f}s")

            # Copy review-inline.txt from work tree if it was created by Claude
            inline_src = os.path.join(work_path, 'review-inline.txt')
            if os.path.exists(inline_src):
                inline_dst = os.path.join(patch_dir, 'review-inline.txt')
                try:
                    shutil.copy(inline_src, inline_dst)
                    log_thread(f"Copied review-inline.txt for {review_id} patch {patch_num}")
                except Exception as e:
                    log_thread(f"Warning: Failed to copy review-inline.txt: {e}")

            # Convert JSON to markdown format
            try:
                convert_json_to_markdown(review_json_path, review_md_path)
                return True
            except Exception as e:
                log_thread(f"Error converting review to markdown: {e}")
                return False

        except subprocess.TimeoutExpired as e:
            elapsed = self.config.claude_timeout
            log_thread(f"Claude review timed out for {review_id} patch {patch_num} after {elapsed}s (attempt {attempt})")

            # Save timeout information for inspection
            timeout_info_path = os.path.join(patch_dir, f'timeout-info-attempt{attempt}.txt')
            with open(timeout_info_path, 'w') as f:
                f.write(f"Attempt: {attempt}\n")
                f.write(f"Claude review timed out after {self.config.claude_timeout} seconds\n")
                f.write(f"Command: {' '.join(cmd)}\n")
                f.write(f"Working directory: {work_path}\n")
                if hasattr(e, 'stderr') and e.stderr:
                    f.write(f"\nStderr output:\n{e.stderr.decode()}\n")

            # Save any partial output
            self._save_partial_output(patch_dir, review_json_path, attempt)
            return False
        except Exception as e:
            log_thread(f"Error running Claude review: {e}")
            # Save error information
            error_path = os.path.join(patch_dir, f'error-attempt{attempt}.txt')
            with open(error_path, 'w') as f:
                f.write(f"Attempt: {attempt}\n")
                f.write(f"Error: {str(e)}\n")
                import traceback
                f.write(traceback.format_exc())
            return False
