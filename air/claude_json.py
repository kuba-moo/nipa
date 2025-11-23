# SPDX-License-Identifier: GPL-2.0

"""
Parse Claude stream-json output and convert to plain text

Based on patterns from:
- claude-code-log (MIT License) - https://github.com/daaain/claude-code-log
- claude-code-sdk-python (MIT License) - https://github.com/anthropics/claude-code-sdk-python
"""

import json
from typing import Iterator


def extract_text_from_stream(stream: Iterator[str]) -> str:
    """Extract plain text from Claude's stream-json format.

    Args:
        stream: Iterator of JSON lines (file object or list of strings)

    Returns:
        Extracted plain text from Claude's response
    """
    text_parts = []

    for line in stream:
        line = line.strip()

        if not line:
            continue

        try:
            data = json.loads(line)

            # Extract text from assistant messages
            if (data.get('type') == 'assistant' and
                'message' in data and
                'content' in data['message']):

                for content_item in data['message']['content']:
                    if content_item.get('type') == 'text':
                        text = content_item.get('text', '')
                        if text:
                            text_parts.append(text)

            # Handle streaming deltas (if Claude uses them)
            elif data.get('type') == 'content_block_delta':
                delta_text = data.get('delta', {}).get('text', '')
                if delta_text:
                    text_parts.append(delta_text)

        except (json.JSONDecodeError, Exception):
            # Silently skip malformed lines
            continue

    return ''.join(text_parts)


def convert_json_to_markdown(json_path: str, markdown_path: str):
    """Convert Claude stream-json output file to markdown.

    Args:
        json_path: Path to input JSON file
        markdown_path: Path to output markdown file
    """
    with open(json_path, 'r') as f:
        text = extract_text_from_stream(f)

    with open(markdown_path, 'w') as f:
        f.write(text)
