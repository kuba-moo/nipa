#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
#
# Copyright (C) 2019 Netronome Systems, Inc.
# Copyright (c) 2020 Facebook

import configparser
import datetime
import json
import os
import time
import shutil

from core import NIPA_DIR
from core import log, log_open_sec, log_end_sec, log_init
from pw import Patchwork
from pw import PwSeries


def worker_done(series_dir, worker, summary=None, status=0):
    if summary is None:
        if status == 0:
            summary = "[OKAY] " + worker
        else:
            summary = "[FAIL] " + worker + " status: " + str(status)

    worker_dir = os.path.join(series_dir, worker)

    summary_file = os.path.join(worker_dir, "summary")
    status_file = os.path.join(worker_dir, "status")

    with open(summary_file, 'w') as fp:
        fp.write(summary)
    with open(status_file, 'w') as fp:
        fp.write(str(status))


def write_raw(directory, contents):
    if isinstance(contents, str):
        contents = contents.encode('utf-8')
    raw = os.path.join(directory, "raw")
    with open(raw, 'wb') as fp:
        fp.write(contents)


def write_out_series(result_dir, series):
    series_dir = os.path.join(result_dir, str(series.id))

    load_dir = os.path.join(series_dir, "load")
    status_file = os.path.join(load_dir, 'status')

    if os.path.exists(series_dir):
        if os.path.exists(status_file):
            log(f"Series {pw_series['id']} already downloaded", "")
            return

        shutil.rmtree(series_dir)
    os.mkdir(series_dir)
    os.mkdir(load_dir)

    if series.cover_letter:
        cover_dir = os.path.join(series_dir, "cover")
        os.mkdir(cover_dir)
        write_raw(cover_dir, series.cover_letter)

    patches_dir = os.path.join(series_dir, "patches")
    os.mkdir(patches_dir)
    for patch in series.patches:
        patch_dir = os.path.join(patches_dir, str(patch.id))
        os.mkdir(patch_dir)
        write_raw(patch_dir, patch.raw_patch)

    worker_done(series_dir, "load")


# Init state

config = configparser.ConfigParser()
config.read(['nipa.config', 'pw.config', 'poller.config'])

log_init(config.get('log', 'type', fallback='org'),
         config.get('log', 'file', fallback=os.path.join(NIPA_DIR,
                                                         "pw_load.org")))

state = {
    'last_poll': str(datetime.datetime.utcnow() - datetime.timedelta(days=3)),
    'last_id': 0,
}

result_dir = config.get('results', 'dir', fallback=os.path.join(NIPA_DIR, "results"))
if not os.path.isdir(result_dir):
    os.mkdir(result_dir)

# Read the state file
try:
    with open('poller.state', 'r') as f:
        loaded = json.load(f)

        for k in state.keys():
            state[k] = loaded[k]
except FileNotFoundError:
    pass

# Prep
pw = Patchwork(config)

partial_series = 0
partial_series_id = 0
prev_time = state['last_poll']

# Loop
try:
    while True:
        poll_ival = 120
        prev_time = state['last_poll']
        prev_time_obj = datetime.datetime.fromisoformat(prev_time)
        since = prev_time_obj - datetime.timedelta(minutes=4)
        state['last_poll'] = str(datetime.datetime.utcnow())

        log_open_sec(f"Checking at {state['last_poll']} since {since}")

        json_resp = pw.get_series_all(since=since)
        log(f"Loaded {len(json_resp)} series", "")

        pw_series = {}
        for pw_series in json_resp:
            log_open_sec(f"Checking series {pw_series['id']} " +
                         f"with {pw_series['total']} patches")

            if pw_series['id'] <= state['last_id']:
                log(f"Already seen {pw_series['id']}", "")
                log_end_sec()
                continue

            s = PwSeries(pw, pw_series)

            log("Series info",
                f"Series ID {s['id']}\n" +
                f"Series title {s['name']}\n" +
                f"Author {s['submitter']['name']}\n" +
                f"Date {s['date']}")
            log_open_sec('Patches')
            for p in s['patches']:
                log(p['name'], "")
            log_end_sec()

            if not s['received_all']:
                if partial_series < 4 or partial_series_id != s['id']:
                    log("Partial series, retrying later", "")
                    try:
                        series_time = datetime.datetime.fromisoformat(s['date'])
                        state['last_poll'] = \
                            str(series_time - datetime.timedelta(minutes=4))
                    except:
                        state['last_poll'] = prev_time
                    poll_ival = 30
                    log_end_sec()
                    break
                else:
                    log("Partial series, happened too many times, ignoring", "")
                    log_end_sec()
                    continue
            log_end_sec()

            write_out_series(result_dir, s)

            state['last_id'] = s['id']

        if state['last_poll'] == prev_time:
            partial_series += 1
            partial_series_id = pw_series['id']
        else:
            partial_series = 0

        time.sleep(poll_ival)
        log_end_sec()
finally:
    # We may have not completed the last poll
    state['last_poll'] = prev_time
    # Dump state
    with open('poller.state', 'w') as f:
        loaded = json.dump(state, f)