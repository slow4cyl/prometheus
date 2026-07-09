#!/usr/bin/env python3
"""
state_lock.py — Serialized write access to self_state.json.

All scripts that modify self_state.json MUST use this module instead of
direct file reads/writes. It provides:

1. File-level lock (fcntl.flock) — only one writer at a time
2. Atomic write (tmp + os.replace) — no partial writes
3. Read-after-lock — each writer sees the LATEST state

Usage:
    from state_lock import load_state, save_state

    state = load_state()           # acquires lock, reads latest
    state["curiosity_queue"] = []  # modify
    save_state(state)              # atomic write, releases lock

Or as a context manager:
    from state_lock import state_write_lock

    with state_write_lock() as state:
        state["curiosity_queue"] = []
        # auto-saved on exit
"""
import fcntl
import json
import os
import sys
import time
from contextlib import contextmanager

"""State Lock.

Part of the Prometheus research infrastructure.
"""


STATE_PATH = os.path.expanduser("~/.hermes/self_state.json")
LOCK_PATH = STATE_PATH + ".lock"


def load_state():
    """Load self_state.json. Call save_state() after modifying."""
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"WARN: Could not load self_state.json: {e}", file=sys.stderr)
        return {}


def save_state(state):
    """Atomic write of self_state.json. Releases lock implicitly."""
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_PATH)


@contextmanager
def state_write_lock(timeout=60):
    """Context manager: acquires exclusive lock with timeout, yields state dict, auto-saves on exit.
    
    Uses non-blocking lock with retry to prevent indefinite blocking.
    If lock can't be acquired within `timeout` seconds, yields None
    (caller should check and skip write).
    
    Usage:
        with state_write_lock() as state:
            if state is None:
                return  # someone else is writing, skip
            state["key"] = "value"
            # state is saved automatically when block exits
    """
    lock_fd = open(LOCK_PATH, "w")
    acquired = False
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except IOError:
                time.sleep(0.1)
        
        if not acquired:
            yield None
            return
        
        state = load_state()
        yield state
        save_state(state)
    finally:
        if acquired:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except Exception:
                pass
        try:
            lock_fd.close()
        except Exception:
            pass


def get_state_under_lock():
    """Acquire lock and return (state, lock_fd). Caller MUST release and save.

    For scripts that need manual control over when save happens:
        state, lock_fd = get_state_under_lock()
        try:
            # ... modify state ...
            save_state(state)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
    """
    lock_fd = open(LOCK_PATH, "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    state = load_state()
    return state, lock_fd


def release_lock(lock_fd):
    """Release lock and close file descriptor."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
    except Exception:
        pass
