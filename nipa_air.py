#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

"""NIPA AIR - AI Review service for kernel patches

This service provides a REST API for reviewing kernel patches using Claude AI.
It supports patches from Patchwork, raw patch text, or existing commit hashes.
"""

import argparse
import configparser
import os
import sys

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

from air.config import AirConfig
from air.auth import TokenAuth
from air.service import AirService
from core import log_init, log


# Global variables for app state
token_auth = None
service = None


def create_app(config_path=None, skip_semcode=False, keep_temp_trees=False):
    """Create and configure the Flask application

    Args:
        config_path: Path to configuration file (if None, uses NIPA_AIR_CONFIG env var)
        skip_semcode: Skip semcode-index (for debugging)
        keep_temp_trees: Keep temporary work trees (for debugging)

    Returns:
        Flask application instance
    """
    global token_auth, service

    # Get config path from environment if not provided
    if config_path is None:
        config_path = os.environ.get('NIPA_AIR_CONFIG')
        if not config_path:
            raise ValueError("Config path must be provided or set in NIPA_AIR_CONFIG environment variable")

    # Load configuration
    config_parser = configparser.ConfigParser()
    config_parser.read(config_path)

    # Initialize configuration
    config = AirConfig(config_parser, skip_semcode=skip_semcode,
                      keep_temp_trees=keep_temp_trees)

    # Initialize logging
    log_init(config_parser.get('log', 'type', fallback='org'),
             config_parser.get('log', 'file', fallback='air.log'))
    log("Starting NIPA AIR service")

    # Initialize service components
    token_auth = TokenAuth(config.token_db_path)
    service = AirService(config, token_auth=token_auth)

    # Create Flask app
    app = Flask(__name__, static_folder='ui', static_url_path='')
    CORS(app)

    # API endpoints
    @app.route('/api/review', methods=['POST'])
    def post_review():
        """Submit a new review request"""
        print("POST /api/review - Request received")
        data = request.get_json()
        print(f"Request data: {data}")

        # Validate token
        token = data.get('token')
        print(f"Token: {token}")
        if not token_auth.validate_token(token):
            print("Token validation failed")
            return jsonify({'error': 'Invalid token'}), 401

        print("Token validated, submitting review...")
        try:
            review_id = service.submit_review(data, token)
            print(f"Review submitted successfully: {review_id}")
            return jsonify({'review_id': review_id}), 200
        except ValueError as e:
            print(f"Validation error: {e}")
            return jsonify({'error': str(e)}), 400
        except Exception as e:
            print(f"Error submitting review: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': 'Internal server error'}), 500

    @app.route('/api/review', methods=['GET'])
    def get_review():
        """Get review status and results"""
        review_id = request.args.get('id')
        token = request.args.get('token')
        fmt = request.args.get('format')

        if not review_id:
            return jsonify({'error': 'Missing review_id'}), 400

        # Token is optional for public_read reviews
        # Validate token if provided
        if token and not token_auth.validate_token(token):
            return jsonify({'error': 'Invalid token'}), 401

        try:
            result = service.get_review(review_id, token, fmt)
            if result is None:
                return jsonify({'error': 'Review not found or access denied'}), 404
            return jsonify(result), 200
        except Exception as e:
            print(f"Error getting review: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': 'Internal server error'}), 500

    @app.route('/api/reviews', methods=['GET'])
    def list_reviews():
        """List recent reviews (for UI)"""
        token = request.args.get('token')
        limit = request.args.get('limit', 50, type=int)
        superuser = request.args.get('superuser', 'false').lower() == 'true'
        public_only = request.args.get('public_only', 'false').lower() == 'true'

        # Token is optional if requesting public_only reviews
        if not public_only:
            if not token or not token_auth.validate_token(token):
                return jsonify({'error': 'Invalid or missing token'}), 401

            # Check if user is actually a superuser if they're requesting superuser mode
            is_superuser = token_auth.is_superuser(token)
            if superuser and not is_superuser:
                return jsonify({'error': 'Superuser access denied'}), 403
        else:
            is_superuser = False

        try:
            reviews = service.list_reviews(token, limit, superuser=superuser and is_superuser,
                                          public_only=public_only)
            return jsonify({'reviews': reviews}), 200
        except Exception as e:
            print(f"Error listing reviews: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': 'Internal server error'}), 500

    @app.route('/api/status', methods=['GET'])
    def get_status():
        """Get service status (for UI)"""
        try:
            status = service.get_status()
            return jsonify(status), 200
        except Exception as e:
            print(f"Error getting status: {e}")
            import traceback
            traceback.print_exc()
            return jsonify({'error': 'Internal server error'}), 500

    @app.route('/')
    def index():
        """Serve the UI"""
        return send_from_directory('ui', 'air.html')

    return app


def main():
    parser = argparse.ArgumentParser(description='NIPA AI Review Service')
    parser.add_argument('config', help='Configuration file path')
    parser.add_argument('--port', type=int, help='Port to listen on (overrides config)')
    parser.add_argument('--dev-skip-semcode', action='store_true', help='[DEV] Skip semcode-index for testing')
    parser.add_argument('--dev-keep-temp-trees', action='store_true', help='[DEV] Keep temporary work trees for debugging')
    args = parser.parse_args()

    # Create app
    app = create_app(args.config, skip_semcode=args.dev_skip_semcode,
                    keep_temp_trees=args.dev_keep_temp_trees)

    # Load config to get port
    config_parser = configparser.ConfigParser()
    config_parser.read(args.config)
    config = AirConfig(config_parser)

    # Start the service
    port = args.port or config.port
    log(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port, threaded=True)


# For gunicorn: Set NIPA_AIR_CONFIG environment variable with config path
# Example: NIPA_AIR_CONFIG=/path/to/air.conf gunicorn -w 4 -b 0.0.0.0:5000 nipa_air:app
app = create_app()


if __name__ == '__main__':
    main()
