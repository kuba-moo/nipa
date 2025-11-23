#!/bin/bash
#
# usage: review-many.sh file_with_shas
#
# file_with_shas is supposed to have one sha per line, and we fire off a parallel
# claude review on every line.
#
# Before starting this, you need to have indexed every sha with semcode
#
# cd linux ; semcode-index -s . --git base..last_sha
#
# This expects cp -a --reflink to work, it makes one copy of the git repo per
# sha.

# Signal handler to kill all background processes on Ctrl+C
cleanup() {
    echo "Received SIGINT, killing all background processes..."
    # Kill all processes in the current process group
    kill 0
    exit 130
}

# Set up signal trap for SIGINT (Ctrl+C)
trap cleanup SIGINT

XARGS_PARALLEL=4

WORKING_DIR=/data/users/clm/working/patch-review
export WORKING_DIR

BINDIR="/data/users/clm/working/bin/"
export BINDIR

MCP_STRING="$WORKING_DIR/mcp-config.json"
export MCP_STRING

BASE_LINUX="$WORKING_DIR/linux"
export BASE_LINUX

JSONPROG="$BINDIR/air/claude-json.py"
export JSONPROG

REVIEW_PROMPT="$WORKING_DIR/review/review-core.md"
export REVIEW_PROMPT

# our claude doesn't seem to have a way to remember it is allowed to use
# these tools, we have to pass it in
#
SEMCODE_ALLOWED="--allowedTools mcp__semcode__find_function,mcp__semcode__diff_functions,mcp__semcode__grep_functions,mcp__semcode__find_callchain,mcp__semcode__find_callers,mcp__semcode__find_calls,mcp__semcode__find_type"
export SEMCODE_ALLOWED

review_one() {
	echo "review_one $1"

	SHA=$1
	echo "Processing $SHA"
	DIR="$BASE_LINUX.$SHA"

	if [ ! -d $DIR ]; then
		cp -a --reflink $BASE_LINUX $DIR
		cp -a $WORKING_DIR/review $DIR
		cd $DIR
		git reset --hard $SHA
	else
		cd $DIR
	fi

	start=$(date +%s)
	rm -f review.json
	rm -f review.md
	rm -f review-inline.txt
	rm -f review.duration.txt

	claude --mcp-config $MCP_STRING --strict-mcp-config $SEMCODE_ALLOWED --model sonnet \
		-p "review the top commit in this directory using prompt $REVIEW_PROMPT" \
		--verbose --output-format=stream-json | tee review.json | $JSONPROG
	end=$(date +%s)
	echo "Elapsed time: $((end - start)) seconds (sha $SHA)" | tee review.duration.txt

	$JSONPROG -i review.json -o review.md
	exit 0
}

export -f review_one

review_timeout() {
	echo "review_timeout $1"
	for i in {1..3}; do
		timeout -k 10 800 bash -c 'review_one $1' _ $1
		rc=$?
	        if [ $rc -ne 124 ] && [ $rc -ne 137 ]; then
			# Command completed (not killed by timeout)
			echo "review $1 completed (exit code $rc) on attempt $i"
			exit 0
	        else
			echo "Review $1 Timeout fired (code $rc) on attempt $i."
	        fi
	done
	echo "review $1 timed out 3 times and failed"
}

export -f review_timeout

if [ -z "$1" ]; then
        echo "usage: review-many <file_with_shas>"
        echo "  file_with_shas: file containing SHA hashes to review"
        exit 1
fi

QUEUE_FILE=$1

awk '{print $1}' $QUEUE_FILE | xargs -n 1 -P $XARGS_PARALLEL bash -c 'review_timeout "$@"' _
