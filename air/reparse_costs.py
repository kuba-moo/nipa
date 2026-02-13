#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

"""Re-parse review costs in metadata.json using the fixed cost extraction.

Usage:
    python3 -m air.reparse_costs <results_path>

Where <results_path> is the base results directory containing metadata.json.
"""

import json
import os
import sys


def extract_cost_from_review(json_path):
    """Extract total cost from the last modelUsage in a stream-json file."""
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
                except (json.JSONDecodeError, ValueError):
                    continue
    except FileNotFoundError:
        return 0.0

    if not last_model_usage:
        return 0.0

    total = 0.0
    for usage in last_model_usage.values():
        if 'costUSD' in usage:
            total += float(usage['costUSD'])
    return total


def reparse_costs(results_path: str):
    metadata_path = os.path.join(results_path, 'metadata.json')

    with open(metadata_path, 'r') as f:
        reviews = json.load(f)

    updated = 0
    for review_id, review in reviews.items():
        token = review.get('token')
        patch_count = review.get('patch_count', 0)

        if not token or patch_count <= 0:
            continue

        review_dir = os.path.join(results_path, token, review_id)
        total_cost = 0.0

        for i in range(1, patch_count + 1):
            review_json = os.path.join(review_dir, str(i), 'review.json')
            if os.path.exists(review_json):
                total_cost += extract_cost_from_review(review_json)

        old_cost = review.get('cost_usd')
        if total_cost > 0:
            new_cost = round(total_cost, 4)
        else:
            new_cost = old_cost  # keep whatever was there if we can't compute

        if old_cost != new_cost:
            review['cost_usd'] = new_cost
            print(f"{review_id}: ${old_cost} -> ${new_cost}")
            updated += 1

    # Write back
    with open(metadata_path, 'w') as f:
        json.dump(reviews, f, indent=2)

    print(f"\nUpdated {updated} reviews in {metadata_path}")


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <results_path>")
        sys.exit(1)

    reparse_costs(sys.argv[1])
