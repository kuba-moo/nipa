# SPDX-License-Identifier: GPL-2.0

"""Logging helper for AIR service

This provides thread-aware logging that prefixes messages with the thread name.
Only use this in worker threads and service modules - the main thread should use
the core logger (log, log_sec, etc.).
"""

import threading


def log_thread(message: str):
    """Log a message with thread name prefix

    Args:
        message: Message to log
    """
    thread_name = threading.current_thread().name
    print(f"[{thread_name}] {message}")


def log_thread_debug(prefix: str, message: str):
    """Log a debug message with thread name and custom prefix

    Args:
        prefix: Custom prefix (e.g., "DEBUG", "INFO")
        message: Message to log
    """
    thread_name = threading.current_thread().name
    print(f"[{thread_name}] {prefix}: {message}")
