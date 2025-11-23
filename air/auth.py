# SPDX-License-Identifier: GPL-2.0

"""Token authentication for AIR service"""

import yaml
from typing import Optional, Dict


class TokenAuth:
    """Token-based authentication handler"""

    def __init__(self, token_db_path: str):
        """Initialize token authentication

        Args:
            token_db_path: Path to YAML file containing token information
        """
        self.token_db_path = token_db_path
        self.tokens: Dict[str, Dict] = {}
        self.load_tokens()

    def load_tokens(self):
        """Load tokens from YAML file"""
        try:
            with open(self.token_db_path, 'r') as f:
                data = yaml.safe_load(f)
                if data and 'tokens' in data:
                    for token_info in data['tokens']:
                        token = token_info['token']
                        self.tokens[token] = {
                            'name': token_info.get('name', ''),
                            'date': token_info.get('date', ''),
                            'superuser': token_info.get('superuser', False)
                        }
        except FileNotFoundError:
            # Create empty token file
            with open(self.token_db_path, 'w') as f:
                yaml.safe_dump({'tokens': []}, f)

    def validate_token(self, token: str) -> bool:
        """Check if token is valid

        Args:
            token: Token string to validate

        Returns:
            True if token is valid, False otherwise
        """
        return token in self.tokens

    def is_superuser(self, token: str) -> bool:
        """Check if token has superuser privileges

        Args:
            token: Token string to check

        Returns:
            True if token is a superuser token, False otherwise
        """
        if token not in self.tokens:
            return False
        return self.tokens[token].get('superuser', False)

    def get_token_info(self, token: str) -> Optional[Dict]:
        """Get information about a token

        Args:
            token: Token string

        Returns:
            Dictionary with token information, or None if not found
        """
        return self.tokens.get(token)
