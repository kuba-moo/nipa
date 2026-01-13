# SPDX-License-Identifier: GPL-2.0

"""Token authentication for AIR service"""

import secrets
from datetime import date
from typing import Optional, Dict
import yaml


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
                            'superuser': token_info.get('superuser', False),
                            'public_read': token_info.get('public_read', False)
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

    def is_public_read(self, token: str) -> bool:
        """Check if token has public_read privileges

        Args:
            token: Token string to check

        Returns:
            True if token has public_read enabled, False otherwise
        """
        if token not in self.tokens:
            return False
        return self.tokens[token].get('public_read', False)

    def get_token_info(self, token: str) -> Optional[Dict]:
        """Get information about a token

        Args:
            token: Token string

        Returns:
            Dictionary with token information, or None if not found
        """
        return self.tokens.get(token)

    def create_token(self, name: str) -> str:
        """Create a new token and save to database

        Args:
            name: Human-readable name/description for the token

        Returns:
            The newly created token string
        """
        # Generate a secure random token
        token = secrets.token_urlsafe(32)

        # Create token info
        token_info = {
            'name': name,
            'date': date.today().isoformat(),
            'superuser': False,
            'public_read': False
        }

        # Add to in-memory store
        self.tokens[token] = token_info

        # Save to YAML file
        self._save_tokens()

        return token

    def _save_tokens(self):
        """Save all tokens to YAML file"""
        token_list = []
        for token, info in self.tokens.items():
            token_entry = {
                'token': token,
                'name': info.get('name', ''),
                'date': info.get('date', ''),
            }
            # Only include optional fields if they're set to True
            if info.get('superuser'):
                token_entry['superuser'] = True
            if info.get('public_read'):
                token_entry['public_read'] = True
            token_list.append(token_entry)

        with open(self.token_db_path, 'w') as f:
            yaml.safe_dump({'tokens': token_list}, f, default_flow_style=False)
