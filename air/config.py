# SPDX-License-Identifier: GPL-2.0

"""Configuration management for AIR service"""

import os
from core import NIPA_DIR


class AirConfig:
    """Configuration container for AIR service"""

    def __init__(self, config, skip_semcode=False, keep_temp_trees=False):
        """Initialize configuration from ConfigParser object

        Args:
            config: ConfigParser object with service configuration
            skip_semcode: Skip semcode-index (for debugging)
            keep_temp_trees: Keep temporary work trees (for debugging)
        """
        self.config = config
        self.skip_semcode = skip_semcode
        self.keep_temp_trees = keep_temp_trees

        # Basic configuration
        self.git_tree = config.get('air', 'git_tree')
        self.max_work_trees = config.getint('air', 'max_work_trees', fallback=4)
        self.max_claude_runs = config.getint('air', 'max_claude_runs', fallback=4)
        self.token_db_path = config.get('air', 'token_db')
        self.results_path = config.get('air', 'results_path',
                                       fallback=os.path.join(NIPA_DIR, 'results', 'air'))
        self.port = config.getint('air', 'port', fallback=5000)

        # MCP configuration
        self.mcp_config = config.get('mcp', 'config')
        self.mcp_tools = config.get('mcp', 'tools')

        # Review configuration
        self.review_prompt_dir = config.get('review', 'prompt_dir')
        self.review_prompt_file = config.get('review', 'prompt_file')

        # Claude configuration
        self.claude_model = config.get('claude', 'model', fallback='sonnet')
        self.claude_timeout = config.getint('claude', 'timeout', fallback=800)
        self.claude_retries = config.getint('claude', 'retries', fallback=3)

        # Ensure results path exists
        os.makedirs(self.results_path, exist_ok=True)
        os.makedirs(os.path.dirname(self.token_db_path), exist_ok=True)
