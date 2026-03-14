# SPDX-License-Identifier: GPL-2.0

"""
Parse Claude stream-json output and convert to plain text

Based on patterns from:
- claude-code-log (MIT License) - https://github.com/daaain/claude-code-log
- claude-code-sdk-python (MIT License) - https://github.com/anthropics/claude-code-sdk-python
"""

import json
import os
import re
import shutil
from typing import Iterator, List, Tuple


def _sanitize_name(description: str) -> str:
    """Sanitize a description string for use as a filename.

    Lowercases and replaces any character not in a-z0-9- with underscore.
    """
    return re.sub(r'[^a-z0-9-]', '_', description.lower())


def _unique_path(directory: str, stem: str, ext: str) -> str:
    """Return a unique file path, appending -1, -2, etc. if needed."""
    path = os.path.join(directory, f'{stem}.{ext}')
    if not os.path.exists(path):
        return path
    num = 1
    while True:
        path = os.path.join(directory, f'{stem}-{num}.{ext}')
        if not os.path.exists(path):
            return path
        num += 1


def parse_stream(stream: Iterator[str]) -> Tuple[str, List[dict]]:
    """Parse Claude's stream-json format, extracting text and agent info.

    Returns:
        (text, agents) where text is the concatenated assistant text and
        agents is a list of agent info dicts from toolUseResult fields.
    """
    text_parts = []
    agents = []

    for line in stream:
        line = line.strip()

        if not line:
            continue

        try:
            data = json.loads(line)
        except (json.JSONDecodeError, Exception):
            continue

        msg_type = data.get('type')

        # Extract text from assistant messages
        if msg_type == 'assistant' and 'message' in data:
            for content_item in data['message'].get('content', []):
                if content_item.get('type') == 'text':
                    text = content_item.get('text', '')
                    if text:
                        text_parts.append(text)

        # Handle streaming deltas
        elif msg_type == 'content_block_delta':
            delta_text = data.get('delta', {}).get('text', '')
            if delta_text:
                text_parts.append(delta_text)

        # Extract agent info from tool_use_result on user messages
        elif msg_type == 'user':
            tur = data.get('tool_use_result')
            if isinstance(tur, dict) and tur.get('agentId'):
                agent = {}
                for key in ('agentId', 'description', 'prompt',
                            'outputFile', 'status'):
                    if key in tur:
                        agent[key] = tur[key]
                agents.append(agent)

    return ''.join(text_parts), agents


def _process_agent_outputs(agents: List[dict], output_dir: str):
    """Recursively process agent outputs into the output directory."""
    for agent in agents:
        description = agent.get('description')
        output_file = agent.get('outputFile')
        if not description or not output_file:
            continue

        name = _sanitize_name(description)

        # Save the raw stream-json
        json_path = _unique_path(output_dir, name, 'json')
        try:
            shutil.copy(output_file, json_path)
        except (FileNotFoundError, Exception):
            continue

        # Parse and write markdown
        md_path = _unique_path(output_dir, name, 'md')
        with open(json_path, 'r') as f:
            agent_text, sub_agents = parse_stream(f)

        with open(md_path, 'w') as f:
            f.write(agent_text)

        # Write sub-agents list and recurse
        if sub_agents:
            agents_path = _unique_path(output_dir, f'{name}-agents', 'json')
            with open(agents_path, 'w') as f:
                json.dump(sub_agents, f, indent=2)
                f.write('\n')
            _process_agent_outputs(sub_agents, output_dir)


def convert_json_to_markdown(json_path: str, output_dir: str):
    """Convert Claude stream-json output to a directory of markdown files.

    Creates output_dir with:
      - main.md: main assistant text
      - main-agents.json: agent list
      - {description}.md: each agent's output (recursively)

    Args:
        json_path: Path to input stream-json file
        output_dir: Path to output directory
    """
    os.makedirs(output_dir, exist_ok=True)

    with open(json_path, 'r') as f:
        text, agents = parse_stream(f)

    with open(os.path.join(output_dir, 'main.md'), 'w') as f:
        f.write(text)

    if agents:
        with open(os.path.join(output_dir, 'main-agents.json'), 'w') as f:
            json.dump(agents, f, indent=2)
            f.write('\n')
        _process_agent_outputs(agents, output_dir)


def extract_cost_from_review(json_path: str) -> float:
    """Extract total cost from Claude stream-json output file.

    The modelUsage objects in the stream are cumulative running totals,
    so we only use the last one and sum the costUSD across models.

    Args:
        json_path: Path to input JSON file

    Returns:
        Total cost in USD, or 0.0 if no cost data found
    """
    last_model_usage = None

    try:
        with open(json_path, 'r') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)

                    if isinstance(data.get('modelUsage'), dict):
                        last_model_usage = data['modelUsage']

                except (json.JSONDecodeError, ValueError, Exception):
                    # Silently skip malformed lines or invalid cost values
                    continue

    except FileNotFoundError:
        # File doesn't exist, return 0.0
        pass

    if not last_model_usage:
        return 0.0

    total_cost = 0.0
    for usage in last_model_usage.values():
        if 'costUSD' in usage:
            total_cost += float(usage['costUSD'])
    return total_cost
