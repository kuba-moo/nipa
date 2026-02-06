#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

"""Purge projects matching a pattern from .claude.json and .claude/projects/"""

import argparse
import fnmatch
import json
import os
import shutil
import sys
from typing import List, Tuple


def load_json(filepath: str) -> dict:
    """Load JSON file

    Args:
        filepath: Path to JSON file

    Returns:
        Parsed JSON data
    """
    with open(filepath, 'r') as f:
        return json.load(f)


def save_json(filepath: str, data: dict):
    """Save JSON file with pretty formatting

    Args:
        filepath: Path to JSON file
        data: Data to save
    """
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')


def path_to_dirname(path: str) -> str:
    """Convert a project path to the directory name format used in .claude/projects/

    Args:
        path: Project path like '/home/user/project'

    Returns:
        Directory name like '-home-user-project'
    """
    return path.replace('/', '-')


def find_matching_projects(projects: dict, pattern: str) -> List[str]:
    """Find project paths matching a pattern

    Args:
        projects: Dictionary of projects (key = path)
        pattern: Glob pattern to match against project paths

    Returns:
        List of matching project paths
    """
    matches = []
    for path in projects.keys():
        if fnmatch.fnmatch(path, pattern):
            matches.append(path)
    return matches


def find_matching_directories(projects_dir: str, pattern: str) -> List[Tuple[str, str]]:
    """Find project directories matching a pattern

    Args:
        projects_dir: Path to .claude/projects/ directory
        pattern: Glob pattern to match against project paths

    Returns:
        List of tuples (original_path, directory_path)
    """
    matches = []
    if not os.path.isdir(projects_dir):
        return matches

    for dirname in os.listdir(projects_dir):
        dir_path = os.path.join(projects_dir, dirname)
        if not os.path.isdir(dir_path):
            continue

        # Convert dirname back to path format for matching
        # e.g., '-home-user-project' -> '/home/user/project'
        if dirname.startswith('-'):
            original_path = dirname.replace('-', '/')
        else:
            original_path = '/' + dirname.replace('-', '/')

        if fnmatch.fnmatch(original_path, pattern):
            matches.append((original_path, dir_path))

    return matches


def main():
    parser = argparse.ArgumentParser(
        description='Purge projects matching a pattern from .claude.json and .claude/projects/',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List projects matching pattern (dry-run)
  %(prog)s --pattern '/home/*/devel/wt-*' --dry-run

  # Delete projects matching pattern
  %(prog)s --pattern '*/wt-*'

  # Only clean JSON, not directories
  %(prog)s --pattern '*/temp-*' --json-only

  # Only clean directories, not JSON
  %(prog)s --pattern '*/temp-*' --dirs-only
        """
    )

    parser.add_argument('--pattern', '-p', required=True,
                       help='Glob pattern to match project paths against')
    parser.add_argument('--dry-run', '-n', action='store_true',
                       help='Show what would be deleted without actually deleting')
    parser.add_argument('--json-only', action='store_true',
                       help='Only clean .claude.json, not project directories')
    parser.add_argument('--dirs-only', action='store_true',
                       help='Only clean project directories, not .claude.json')
    parser.add_argument('--claude-json', default=os.path.expanduser('~/.claude.json'),
                       help='Path to .claude.json (default: ~/.claude.json)')
    parser.add_argument('--projects-dir', default=os.path.expanduser('~/.claude/projects'),
                       help='Path to projects directory (default: ~/.claude/projects)')

    args = parser.parse_args()

    if args.json_only and args.dirs_only:
        print("Error: Cannot specify both --json-only and --dirs-only", file=sys.stderr)
        sys.exit(1)

    json_matches = []
    dir_matches = []

    # Find matching entries in .claude.json
    if not args.dirs_only:
        if os.path.exists(args.claude_json):
            try:
                data = load_json(args.claude_json)
                projects = data.get('projects', {})
                if isinstance(projects, dict):
                    json_matches = find_matching_projects(projects, args.pattern)
            except json.JSONDecodeError as e:
                print(f"Error: Invalid JSON in {args.claude_json}: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"Warning: {args.claude_json} not found", file=sys.stderr)

    # Find matching directories
    if not args.json_only:
        if os.path.isdir(args.projects_dir):
            dir_matches = find_matching_directories(args.projects_dir, args.pattern)
        else:
            print(f"Warning: {args.projects_dir} not found", file=sys.stderr)

    # Report findings
    total_matches = len(json_matches) + len(dir_matches)
    if total_matches == 0:
        print(f"No projects matching pattern '{args.pattern}'")
        sys.exit(0)

    if json_matches:
        print(f"Found {len(json_matches)} project(s) in .claude.json matching '{args.pattern}':")
        for path in json_matches:
            print(f"  - {path}")

    if dir_matches:
        print(f"Found {len(dir_matches)} directory(ies) matching '{args.pattern}':")
        for original_path, dir_path in dir_matches:
            print(f"  - {original_path} ({dir_path})")

    if args.dry_run:
        print("\nDry run - no changes made")
        sys.exit(0)

    # Perform deletions
    deleted_json = 0
    deleted_dirs = 0

    # Delete from .claude.json
    if json_matches and not args.dirs_only:
        data = load_json(args.claude_json)
        projects = data.get('projects', {})
        for path in json_matches:
            if path in projects:
                del projects[path]
                deleted_json += 1
        data['projects'] = projects
        save_json(args.claude_json, data)

    # Delete directories
    if dir_matches and not args.json_only:
        for original_path, dir_path in dir_matches:
            try:
                shutil.rmtree(dir_path)
                deleted_dirs += 1
                print(f"Deleted directory: {dir_path}")
            except Exception as e:
                print(f"Error deleting {dir_path}: {e}", file=sys.stderr)

    # Summary
    print(f"\nDeleted {deleted_json} entry(ies) from .claude.json")
    print(f"Deleted {deleted_dirs} directory(ies) from {args.projects_dir}")


if __name__ == '__main__':
    main()
