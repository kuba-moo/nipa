#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

"""NIPA AIR Upload - AIR to Patchwork synchronization service

Polls AIR for public reviews and posts check results to Patchwork
for matching series.
"""

import argparse
import configparser
import json
import os
import sys
import time
import requests
from datetime import datetime, UTC
from typing import Optional, Dict, List

from core import NIPA_DIR, log, log_init
from pw import Patchwork


class AirPatchworkSync:
    """Synchronize AIR reviews to Patchwork checks"""

    def __init__(self, config_path: str):
        """Initialize sync service

        Args:
            config_path: Path to configuration file
        """
        self.config = configparser.ConfigParser()
        self.config.read([config_path, "nipa.config"])

        # AIR configuration
        self.air_url = self.config.get('air', 'url')
        self.air_server = self.config.get('air', 'server', fallback=self.air_url)
        self.air_token = self.config.get('air', 'token', fallback=None)

        # Patchwork configuration
        self.check_name = self.config.get('patchwork', 'check_name', fallback='ai-review')

        # Service configuration
        self.poll_interval = self.config.getint('service', 'poll_interval', fallback=300)
        self.state_file = self.config.get('service', 'state_file',
                                         fallback='nipa-air-upload.state')

        # Initialize logging
        log_dir = self.config.get('log', 'dir', fallback=NIPA_DIR)
        log_init(self.config.get('log', 'type', fallback='org'),
                 self.config.get('log', 'file', fallback=os.path.join(log_dir, "air-upload.org")),
                 force_single_thread=True)


        # Initialize Patchwork client
        self.patchwork = Patchwork(self.config)

        # Load state
        self.uploaded_reviews = self.load_state()

    def load_state(self) -> set:
        """Load set of already uploaded review IDs from state file

        Returns:
            Set of review IDs that have been uploaded
        """
        if not os.path.exists(self.state_file):
            return set()

        try:
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                return set(state.get('uploaded_reviews', []))
        except Exception as e:
            print(f"Error loading state file: {e}")
            return set()

    def save_state(self, uploaded_reviews: set):
        """Save set of uploaded review IDs to state file

        Args:
            uploaded_reviews: Set of review IDs that have been uploaded
        """
        state = {
            'uploaded_reviews': list(uploaded_reviews),
            'last_update': datetime.now(UTC).isoformat(),
            'count': len(uploaded_reviews)
        }

        try:
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            print(f"Error saving state file: {e}")

    def get_public_reviews(self) -> List[Dict]:
        """Fetch public reviews from AIR

        Returns:
            List of review dictionaries
        """
        try:
            url = f"{self.air_url}/api/reviews?public_only=true&limit=100"
            if self.air_token:
                url += f"&token={self.air_token}"
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            data = response.json()
            return data.get('reviews', [])
        except Exception as e:
            print(f"Error fetching public reviews from AIR: {e}")
            return []

    def get_review_details(self, review_id: str) -> Optional[Dict]:
        """Fetch full review details from AIR

        Args:
            review_id: Review ID

        Returns:
            Review details dictionary or None
        """
        try:
            url = f"{self.air_url}/api/review?id={review_id}&format=inline"
            if self.air_token:
                url += f"&token={self.air_token}"
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"Error fetching review {review_id}: {e}")
            return None

    def has_review_comments(self, review_data: Dict) -> bool:
        """Check if review has any inline comments

        Args:
            review_data: Review data with 'review' field

        Returns:
            True if any patch has review comments
        """
        reviews = review_data.get('review', [])
        for review in reviews:
            if review and review.strip():
                return True
        return False

    def post_patchwork_check(self, series_id: int, review_id: str,
                            has_comments: bool) -> bool:
        """Post check result to Patchwork

        Args:
            series_id: Patchwork series ID
            review_id: AIR review ID
            has_comments: Whether review has comments

        Returns:
            True if successful
        """
        # Determine check status
        state = 'warning' if has_comments else 'success'

        # Build check URL
        check_url = f"{self.air_server}/ai-review.html?id={review_id}"

        # Build check description
        desc = 'AI review completed' if not has_comments else 'AI review found issues'

        try:
            # Fetch series to get individual patches
            series = self.patchwork.get('series', series_id)
            patches = series.get('patches', [])

            if not patches:
                print(f"  Warning: Series {series_id} has no patches")
                return False

            print(f"  Posting check to {len(patches)} patches in series {series_id}: {state}")

            # Post check to each patch in the series
            for patch in patches:
                patch_id = patch['id']
                self.patchwork.post_check(patch=patch_id, name=self.check_name,
                                         state=state, url=check_url, desc=desc)

            return True
        except Exception as e:
            print(f"  Error posting check to Patchwork: {e}")
            return False

    def process_review(self, review: Dict) -> bool:
        """Process a single review

        Args:
            review: Review summary from AIR

        Returns:
            True if processed (whether matched or not)
        """
        review_id = review.get('review_id')
        status = review.get('status')

        print(f"Processing review {review_id} (status: {status})")

        # Only process completed reviews
        if status != 'done':
            print(f"  Skipping: status is {status}, not done")
            return False

        # Get full review details
        review_data = self.get_review_details(review_id)
        if not review_data:
            print(f"  Error: Could not fetch review details")
            return False

        # Check if review has patchwork series ID
        pw_series_id = review_data.get('patchwork_series_id')
        if not pw_series_id:
            print(f"  Skipping: No patchwork series ID")
            return True

        print(f"  Patchwork series ID: {pw_series_id}")

        # Check if review has comments
        has_comments = self.has_review_comments(review_data)
        print(f"  Has review comments: {has_comments}")

        # Post check to Patchwork
        success = self.post_patchwork_check(pw_series_id, review_id, has_comments)

        if success:
            print(f"  Successfully posted check to Patchwork")

        return True

    def run_once(self):
        """Run one sync iteration"""
        print(f"\n[{datetime.now(UTC).isoformat()}] Polling for new reviews...")

        # Fetch public reviews (100 most recent)
        reviews = self.get_public_reviews()
        if not reviews:
            print("No reviews found")
            return

        print(f"Found {len(reviews)} public reviews")
        api_returned_full_set = len(reviews) >= 100

        # Collect review IDs from this fetch
        fetched_review_ids = {r.get('review_id') for r in reviews if r.get('review_id')}

        # Filter to reviews we haven't uploaded yet
        new_reviews = [r for r in reviews if r.get('review_id') not in self.uploaded_reviews]

        if not new_reviews:
            print("No new reviews to process")
            # Still update state to trim if needed
            if api_returned_full_set:
                # Trim state to only reviews we saw in this fetch
                self.uploaded_reviews &= fetched_review_ids
                self.save_state(self.uploaded_reviews)
                print(f"Trimmed state to {len(self.uploaded_reviews)} reviews")
            return

        print(f"Processing {len(new_reviews)} new reviews (already uploaded: {len(self.uploaded_reviews)})...")

        # Track newly uploaded reviews in this run
        newly_uploaded = set()

        # Process each review
        for review in new_reviews:
            review_id = review.get('review_id')
            if not review_id:
                continue

            try:
                processed = self.process_review(review)
                if processed:
                    newly_uploaded.add(review_id)
            except Exception as e:
                print(f"Error processing review {review_id}: {e}")
                import traceback
                traceback.print_exc()

        # Update uploaded reviews set
        if newly_uploaded:
            self.uploaded_reviews.update(newly_uploaded)
            print(f"Uploaded {len(newly_uploaded)} new reviews")

        # Trim state if API returned full set (100 reviews)
        # This prevents unbounded growth of state file
        if api_returned_full_set:
            old_count = len(self.uploaded_reviews)
            self.uploaded_reviews &= fetched_review_ids
            trimmed = old_count - len(self.uploaded_reviews)
            if trimmed > 0:
                print(f"Trimmed {trimmed} old reviews from state")

        # Save state
        self.save_state(self.uploaded_reviews)
        print(f"State updated: tracking {len(self.uploaded_reviews)} uploaded reviews")

    def run(self):
        """Run sync service continuously"""
        print(f"Starting NIPA AIR Upload service")
        print(f"  AIR URL: {self.air_url}")
        print(f"  Check name: {self.check_name}")
        print(f"  Poll interval: {self.poll_interval}s")
        print(f"  State file: {self.state_file}")

        if self.uploaded_reviews:
            print(f"  Already uploaded: {len(self.uploaded_reviews)} reviews")
        else:
            print(f"  No previous state found (will process all reviews)")

        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\nShutting down...")
                break
            except Exception as e:
                print(f"Error in sync loop: {e}")
                import traceback
                traceback.print_exc()

            print(f"\nSleeping for {self.poll_interval} seconds...")
            time.sleep(self.poll_interval)


def main():
    parser = argparse.ArgumentParser(
        description='Upload AIR reviews to Patchwork as checks',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Configuration file format:

[air]
url = https://air.example.com
server = https://air.example.com  # Optional, defaults to url
token = your_air_token  # Optional, for authenticated access

[patchwork]
# Standard patchwork config
server = patchwork.kernel.org
use_ssl = true
token = your_patchwork_token
user = your_patchwork_user_id

# Sync-specific config
check_name = ai-review  # Optional, default: ai-review

[service]
poll_interval = 300  # Optional, default: 300 seconds
state_file = nipa-air-upload.state  # Optional
        """
    )

    parser.add_argument('config', help='Path to configuration file')
    parser.add_argument('--once', action='store_true',
                       help='Run once and exit (no continuous polling)')

    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"Error: Config file not found: {args.config}")
        return 1

    try:
        sync = AirPatchworkSync(args.config)

        if args.once:
            sync.run_once()
        else:
            sync.run()

        return 0
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
