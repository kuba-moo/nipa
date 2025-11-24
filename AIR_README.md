# NIPA AIR - AI Review Service

NIPA AIR is a Flask-based REST service for reviewing kernel patches using Claude AI.

## Features

- Review patches from Patchwork, raw patch text, or existing commit hashes
- Parallel review execution with configurable limits
- Work tree management for concurrent processing
- Semcode integration for code analysis
- Simple web UI for submitting and tracking reviews
- Token-based authentication
- Persistent queue and metadata storage

## Installation

### Requirements

- Python 3.7+
- Flask
- Flask-CORS
- PyYAML
- Git
- Claude CLI
- semcode-index (with MCP integration)

### Setup

1. Install Python dependencies:
```bash
pip install flask flask-cors pyyaml
```

2. Copy and customize the configuration file:
```bash
cp air.conf.example air.conf
# Edit air.conf with your paths and settings
```

3. Create a tokens file:
```bash
cp tokens.yaml.example tokens.yaml
# Edit tokens.yaml to add API tokens
```

4. Ensure the following are set up:
   - A git repository at the path specified in `git_tree`
   - Claude CLI installed and configured
   - semcode-index available in PATH
   - MCP configuration file
   - Review prompt file

## Running the Service

```bash
./nipa_air.py air.conf
```

Or specify a custom port:
```bash
./nipa_air.py air.conf --port 8080
```

## API Endpoints

### POST /api/review

Submit a new review request.

**Request body:**
```json
{
  "token": "your-api-token",
  "tree": "netdev/net",
  "branch": "main",  // optional
  "hash": "abc123..def456",  // OR single hash, OR
  "patchwork_series_id": 12345,  // OR
  "patches": ["patch content"],  // exactly one of hash/patchwork/patches required
  "mask": [true, false, true]  // optional, skip review for false entries
}
```

**Response:**
```json
{
  "review_id": "uuid-string"
}
```

### GET /api/review

Get review status and results.

**Query parameters:**
- `id`: Review ID (required)
- `token`: API token (optional for public_read reviews)
- `format`: Format for results - `json`, `markup`, or `inline` (optional)

**Response (without format):**
```json
{
  "review_id": "uuid",
  "status": "queued|in-progress|done|error",
  "tree": "netdev/net",
  "patchwork_series_id": 12345,  // if from patchwork
  "hash": "abc123..def456",  // if from hash/range
  "date": "2025-01-23T10:00:00",
  "start": "2025-01-23T10:01:00",  // if in-progress or done (setup started)
  "start-llm": "2025-01-23T10:02:00",  // if in-progress or done (Claude started)
  "end": "2025-01-23T10:15:00",  // if done or error (processing completed)
  "queue-len": 5,  // if queued - number of patches ahead
  "message": "error or warning message"  // if error
}
```

**Response (with format, when done):**
```json
{
  // ... same as above ...
  "review": [
    "review content for patch 1",
    null,  // if masked
    "review content for patch 3"
  ]
}
```

### GET /api/reviews

List recent reviews for the authenticated token.

**Query parameters:**
- `token`: API token (required unless public_only=true)
- `limit`: Max reviews to return (default: 50)
- `public_only`: If true, return only public_read reviews (default: false)

### GET /api/status

Get service status (no authentication required).

**Response:**
```json
{
  "service": "air",
  "status": "running",
  "queue_size": 10,
  "max_work_trees": 4,
  "max_claude_runs": 4,
  "review_counts": {
    "queued": 5,
    "in-progress": 2,
    "done": 150,
    "error": 3
  }
}
```

## Web UI

Access the web UI at `http://localhost:5000/` (or your configured port).

The UI provides:
- Service status dashboard
- Review submission form
- Review query interface
- Recent reviews list

## Configuration

See `air.conf.example` for all configuration options.

### Key Configuration Sections

- `[air]`: Basic service settings (paths, limits, port)
- `[mcp]`: MCP/semcode configuration
- `[review]`: Review prompt path
- `[claude]`: Claude CLI settings (model, timeout, retries)
- `[patchwork]`: Optional Patchwork integration

## File Storage Structure

Reviews are stored in the following structure:

```
{results_path}/
├── metadata.json          # Service metadata
├── queue.json             # Persistent queue
└── {token}/
    └── {review_id}/
        ├── message        # Error/warning messages (if any)
        ├── 1/             # First patch (1-based numbering)
        │   ├── patch
        │   ├── review.json
        │   ├── review.md
        │   └── review-inline.txt
        ├── 2/             # Second patch
        │   └── ...
        └── ...
```

## Architecture

### Components

- **Main Service** (`nipa_air.py`): Flask app with API endpoints
- **Config** (`air/config.py`): Configuration management
- **Auth** (`air/auth.py`): Token-based authentication
- **Storage** (`air/storage.py`): File storage and metadata management
- **Queue** (`air/queue.py`): Persistent review queue (incoming requests)
- **TempCopyQueue** (`air/temp_copy_queue.py`): Queue connecting setup and LLM workers
- **WorkTree** (`air/worktree.py`): Git work tree management
- **SetupWorker** (`air/setup_worker.py`): Handles git operations and prep
- **LLMWorker** (`air/llm_worker.py`): Runs Claude reviews
- **WorkerPool** (`air/worker_pool.py`): Manages both worker pools
- **Service** (`air/service.py`): Main orchestrator

### Two-Stage Worker Architecture

The service uses a two-stage worker pool architecture to maximize parallelism:

**Stage 1: Setup Workers**
- One setup worker per work tree (configured via `max_work_trees`)
- Each setup worker has a dedicated work tree (wt-1, wt-2, wt-3, etc.)
- Setup workers pull entire review requests from the review queue
- They perform all git operations:
  - Fetch remote
  - Apply patches (or verify hash range)
  - Run semcode-index on the commit range
  - Create temporary work tree copies for each commit
- Temp copies are queued to TempCopyQueue for LLM workers to process
- No need to acquire/release work trees - each setup worker owns its tree

**Stage 2: LLM Workers**
- Independent pool of LLM workers (configured via `max_claude_runs`)
- LLM workers pull temp copy info from TempCopyQueue
- They run Claude review on the temp copy and save results
- After review completes (success or failure), they clean up the temp copy
- LLM workers have no git tree management - they just run Claude

**TempCopyQueue (Pipeline):**
- Connects setup workers to LLM workers
- Has a max size (2x `max_claude_runs`) to prevent setup workers from
  getting too far ahead of LLM workers
- Setup workers block if queue is full (provides backpressure)

**Benefits:**
- Setup workers can prepare multiple commits in parallel while LLM workers
  independently process them
- Maximizes utilization of both git work trees and Claude API quota
- Allows `max_claude_runs` > `max_work_trees` for better throughput
- Setup operations don't block Claude execution and vice versa

## Troubleshooting

### Work Trees Not Created

Ensure `cp -a --reflink` works on your filesystem (requires CoW support like btrfs or XFS).

### Reviews Timing Out

Increase `claude.timeout` in configuration or check Claude CLI setup.

### Semcode Indexing Fails

Verify semcode-index is installed and MCP configuration is correct.

### Patchwork Integration Issues

Check patchwork configuration and ensure network connectivity to the patchwork server.

## License

GPL-2.0
