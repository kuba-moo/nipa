# SPDX-License-Identifier: GPL-2.0

"""LLM worker for running Claude reviews"""

import os
import shlex
import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass
from typing import Dict, List

from core import log_init
from .log_helper import log_thread, log_thread_debug
from .claude_json import convert_json_to_markdown


@dataclass
class ReviewContext:
    """Context for a single review attempt"""
    work_path: str
    patch_dir: str
    review_id: str
    patch_num: int
    commit_hash: str
    git_range: str
    attempt: int
    model: str
    llm_mode: str

    # Computed paths
    work_prompt_dir: str = ""
    prompt_path: str = ""
    full_prompt_path: str = ""
    review_json_path: str = ""
    review_md_path: str = ""


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
        log_init("stdout", "")
        log_thread(f"LLM worker {worker_id} started")

        while True:
            temp_copy_info = temp_copy_queue.get(timeout=1)
            if temp_copy_info is None:
                continue

            review_id = temp_copy_info['review_id']
            patch_num = temp_copy_info['patch_num']
            log_thread(f"LLM worker {worker_id} processing review {review_id} patch {patch_num}")

            try:
                self._process_temp_copy(temp_copy_info)
            except Exception as e:
                log_thread(f"Error in LLM worker {worker_id} processing {review_id} patch {patch_num}: {e}")
                traceback.print_exc()
            finally:
                temp_path = temp_copy_info['temp_path']
                if not self.config.keep_temp_trees:
                    self.worktree_mgr.remove_temp_copy(temp_path)
                else:
                    log_thread(f"Keeping temp work tree: {temp_path} (--dev-keep-temp-trees)")
                temp_copy_queue.task_done()

    def _process_temp_copy(self, temp_copy_info: Dict):
        """Process a temp copy (run Claude review with retries)"""
        temp_path = temp_copy_info['temp_path']
        token = temp_copy_info['token']
        review_id = temp_copy_info['review_id']
        patch_num = temp_copy_info['patch_num']
        commit_hash = temp_copy_info['commit_hash']
        git_range = temp_copy_info.get('git_range', f"{commit_hash}^..{commit_hash}")

        # Get metadata for model and llm_mode
        metadata = self.storage.get_review_metadata(review_id)
        model = metadata.get('model', self.config.claude_model)
        llm_mode = metadata.get('llm_mode', 'classic')
        patch_dir = self.storage.get_patch_dir(token, review_id, patch_num)

        # Build context
        ctx = ReviewContext(
            work_path=temp_path,
            patch_dir=patch_dir,
            review_id=review_id,
            patch_num=patch_num,
            commit_hash=commit_hash,
            git_range=git_range,
            attempt=0,
            model=model,
            llm_mode=llm_mode,
        )

        # Run with retries
        success = False
        for attempt in range(1, self.config.claude_retries + 1):
            ctx.attempt = attempt
            log_thread(f"Review attempt {attempt} for commit {commit_hash[:8]}")

            success = self._run_review(ctx)
            if success:
                log_thread(f"Successfully reviewed patch {patch_num} for {review_id}")
                break

            log_thread(f"Review attempt {attempt} failed for {commit_hash[:8]}")

        if not success:
            log_thread(f"Review failed after {self.config.claude_retries} attempts for {commit_hash[:8]}")

        self.storage.mark_patch_complete(review_id, patch_num, success)

    def _run_review(self, ctx: ReviewContext) -> bool:
        """Run a single review attempt

        Returns:
            True if successful, False otherwise
        """
        os.makedirs(ctx.patch_dir, exist_ok=True)

        # Prepare prompt directory
        if not self._prepare_prompt_directory(ctx):
            return False

        # Run mode-specific setup (e.g., create_changes.py for orc mode)
        if not self._run_mode_setup(ctx):
            return False

        # Build and execute Claude command
        cmd = self._build_claude_command(ctx)
        self._write_command_info(ctx, cmd)

        return self._execute_claude(ctx, cmd)

    def _prepare_prompt_directory(self, ctx: ReviewContext) -> bool:
        """Copy prompt directory to work tree and set up paths"""
        prompt_dir = self.config.review_prompt_dir.rstrip('/')
        prompt_dir_basename = os.path.basename(prompt_dir)
        ctx.work_prompt_dir = os.path.join(ctx.work_path, prompt_dir_basename)

        log_thread(f"Copying prompt directory to {ctx.work_prompt_dir}")

        # Remove if it exists (from a previous attempt)
        if os.path.exists(ctx.work_prompt_dir):
            log_thread(f"  Removing existing: {ctx.work_prompt_dir}")
            shutil.rmtree(ctx.work_prompt_dir)

        try:
            shutil.copytree(prompt_dir, ctx.work_prompt_dir)
        except Exception as e:
            log_thread(f"Failed to copy prompt directory: {e}")
            self._save_error(ctx, f"Failed to copy prompt directory: {e}")
            return False

        # Set prompt path based on mode
        if ctx.llm_mode == 'orc':
            ctx.prompt_path = os.path.join(prompt_dir_basename, self.config.orc_prompt_file)
        else:
            ctx.prompt_path = os.path.join(prompt_dir_basename, self.config.review_prompt_file)

        ctx.full_prompt_path = os.path.join(ctx.work_path, ctx.prompt_path)
        ctx.review_json_path = os.path.join(ctx.patch_dir, 'review.json')
        ctx.review_md_path = os.path.join(ctx.patch_dir, 'review.md')

        if not os.path.exists(ctx.full_prompt_path):
            log_thread_debug("WARNING", f"  Prompt NOT found: {ctx.full_prompt_path}")

        return True

    def _run_mode_setup(self, ctx: ReviewContext) -> bool:
        """Run mode-specific setup (e.g., create_changes.py for orc mode)"""
        if ctx.llm_mode != 'orc':
            return True

        script_path = os.path.join(ctx.work_prompt_dir, self.config.create_changes_script)
        log_thread(f"Running create_changes.py for commit {ctx.commit_hash}")

        try:
            result = subprocess.run(
                [script_path, ctx.commit_hash],
                cwd=ctx.work_path,
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode != 0:
                log_thread(f"create_changes.py failed: {result.stderr}")
                self._save_error(
                    ctx,
                    f"create_changes.py failed with exit code {result.returncode}\n"
                    f"stdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}\n",
                    prefix="create-changes-error"
                )
                return False
            return True

        except subprocess.TimeoutExpired:
            log_thread("create_changes.py timed out")
            self._save_error(ctx, "create_changes.py timed out after 120 seconds\n",
                           prefix="create-changes-error")
            return False

        except Exception as e:
            log_thread(f"create_changes.py error: {e}")
            self._save_error(ctx, f"create_changes.py error: {e}\n",
                           prefix="create-changes-error")
            return False

    def _build_prompt_message(self, ctx: ReviewContext) -> str:
        """Build the prompt message for Claude"""
        if ctx.llm_mode == 'orc':
            return f"""
            Current directory is the root of a Linux Kernel git repository.
            Read the prompt from {ctx.full_prompt_path} and run it on the {ctx.commit_hash} commit, which is part of a series with git range {ctx.git_range}.
            """
        else:
            return f"""
            Current directory is the root of a Linux Kernel git repository.
            Read the prompt from {ctx.full_prompt_path}.
            Using the prompt, do a deep dive regression analysis of the {ctx.commit_hash} commit.
            Use commit range {ctx.git_range} for the false-positive-guide.md section.
            """

    def _build_claude_command(self, ctx: ReviewContext) -> List[str]:
        """Build the Claude CLI command"""
        prompt_msg = self._build_prompt_message(ctx)
        return [
            'claude',
            '--mcp-config', self.config.mcp_config,
            '--strict-mcp-config',
            '--allowedTools', self.config.mcp_tools,
            '--model', ctx.model,
            '-p', prompt_msg,
            '--verbose',
            '--output-format=stream-json'
        ]

    def _write_command_info(self, ctx: ReviewContext, cmd: List[str]):
        """Write command info file for debugging"""
        info_path = os.path.join(ctx.patch_dir, f'cmd-info{ctx.attempt}.txt')
        with open(info_path, 'w') as f:
            f.write(f"Claude cwd: {ctx.work_path}\n")
            f.write(f"Prompt: {ctx.full_prompt_path}\n")
            f.write(f"Model: {ctx.model}\n")
            f.write(f"LLM mode: {ctx.llm_mode}\n")
            f.write(f"Git range: {ctx.git_range}\n")

            # Get commit reference
            try:
                result = subprocess.run(
                    ['git', 'show', '-s', '--format=reference'],
                    cwd=ctx.work_path, capture_output=True, text=True, check=True
                )
                f.write(f"Commit: {result.stdout.strip()}\n")
            except subprocess.CalledProcessError:
                pass

            f.write("\nCommand:\n")
            f.write(" ".join(shlex.quote(arg) for arg in cmd))
            f.write("\n")

        log_thread(f"Launch Claude (see {info_path})")

    def _execute_claude(self, ctx: ReviewContext, cmd: List[str]) -> bool:
        """Execute Claude and handle results"""
        try:
            # Record LLM start time (only on first attempt of first patch)
            if ctx.attempt == 1 and ctx.patch_num == 1:
                self.storage.set_llm_start_time(ctx.review_id)

            start_time = time.time()
            with open(ctx.review_json_path, 'w') as json_file:
                result = subprocess.run(
                    cmd,
                    cwd=ctx.work_path,
                    stdout=json_file,
                    stderr=subprocess.PIPE,
                    timeout=self.config.claude_timeout
                )
            elapsed = time.time() - start_time

            if result.returncode != 0:
                return self._handle_claude_failure(ctx, result, elapsed)

            log_thread(f"Claude review completed for {ctx.review_id} patch {ctx.patch_num} in {elapsed:.1f}s")
            return self._collect_review_outputs(ctx)

        except subprocess.TimeoutExpired as e:
            return self._handle_timeout(ctx, e)

        except Exception as e:
            log_thread(f"Error running Claude review: {e}")
            self._save_error(ctx, f"Error: {str(e)}\n{traceback.format_exc()}")
            return False

    def _handle_claude_failure(self, ctx: ReviewContext, result, elapsed: float) -> bool:
        """Handle Claude process failure"""
        log_thread(f"Claude review failed for {ctx.review_id} patch {ctx.patch_num} "
                  f"after {elapsed:.1f}s: {result.stderr.decode()}")

        # Save stderr
        stderr_path = os.path.join(ctx.patch_dir, f'claude-stderr-attempt{ctx.attempt}.txt')
        with open(stderr_path, 'w') as f:
            f.write(result.stderr.decode())

        self._save_partial_output(ctx)
        return False

    def _handle_timeout(self, ctx: ReviewContext, exc: subprocess.TimeoutExpired) -> bool:
        """Handle Claude timeout"""
        log_thread(f"Claude review timed out for {ctx.review_id} patch {ctx.patch_num} "
                  f"after {self.config.claude_timeout}s (attempt {ctx.attempt})")

        timeout_info_path = os.path.join(ctx.patch_dir, f'timeout-info-attempt{ctx.attempt}.txt')
        with open(timeout_info_path, 'w') as f:
            f.write(f"Attempt: {ctx.attempt}\n")
            f.write(f"Claude review timed out after {self.config.claude_timeout} seconds\n")
            f.write(f"Working directory: {ctx.work_path}\n")
            if hasattr(exc, 'stderr') and exc.stderr:
                f.write(f"\nStderr output:\n{exc.stderr.decode()}\n")

        self._save_partial_output(ctx)
        return False

    def _collect_review_outputs(self, ctx: ReviewContext) -> bool:
        """Copy review outputs from work tree to patch directory"""
        # Copy review-inline.txt if created
        inline_src = os.path.join(ctx.work_path, 'review-inline.txt')
        if os.path.exists(inline_src):
            inline_dst = os.path.join(ctx.patch_dir, 'review-inline.txt')
            try:
                shutil.copy(inline_src, inline_dst)
                log_thread(f"Copied review-inline.txt for {ctx.review_id} patch {ctx.patch_num}")
            except Exception as e:
                log_thread(f"Warning: Failed to copy review-inline.txt: {e}")

        # Copy review-metadata.json if created
        metadata_src = os.path.join(ctx.work_path, 'review-metadata.json')
        if os.path.exists(metadata_src):
            metadata_dst = os.path.join(ctx.patch_dir, 'review-metadata.json')
            try:
                shutil.copy(metadata_src, metadata_dst)
                log_thread(f"Copied review-metadata.json for {ctx.review_id} patch {ctx.patch_num}")
            except Exception as e:
                log_thread(f"Warning: Failed to copy review-metadata.json: {e}")

        # Convert JSON to markdown
        try:
            convert_json_to_markdown(ctx.review_json_path, ctx.review_md_path)
            return True
        except Exception as e:
            log_thread(f"Error converting review to markdown: {e}")
            return False

    def _save_partial_output(self, ctx: ReviewContext):
        """Save partial review output if available"""
        if not os.path.exists(ctx.review_json_path) or os.path.getsize(ctx.review_json_path) == 0:
            return

        partial_json_path = os.path.join(ctx.patch_dir, f'review-partial-attempt{ctx.attempt}.json')
        try:
            shutil.copy(ctx.review_json_path, partial_json_path)
            log_thread(f"Partial output saved to {partial_json_path}")
        except Exception:
            pass

        # Try to convert partial JSON to markdown (best effort)
        try:
            partial_md_path = os.path.join(ctx.patch_dir, f'review-partial-attempt{ctx.attempt}.md')
            convert_json_to_markdown(ctx.review_json_path, partial_md_path)
        except Exception:
            pass

    def _save_error(self, ctx: ReviewContext, message: str, prefix: str = "error"):
        """Save error information to file"""
        error_path = os.path.join(ctx.patch_dir, f'{prefix}-attempt{ctx.attempt}.txt')
        with open(error_path, 'w') as f:
            f.write(f"Attempt: {ctx.attempt}\n")
            f.write(message)
