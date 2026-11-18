"""Control for bench/load (`run.py hook`): a no-op Python profile hook on every thread.

No pymolt involved. It measures what any Python-level ``sys.setprofile`` hook costs this
service on this host (per-call overhead plus the GIL hand-off effects it triggers) before
the watcher does any work of its own, so a capture's loss can be split into "platform"
and "pymolt".
"""
import sys
import threading


def _noop(frame, event, arg):
    return None


threading.setprofile(_noop)
sys.setprofile(_noop)
