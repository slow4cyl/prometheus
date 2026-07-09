#!/usr/bin/env python3
"""
Prometheus Working Memory Daemon
Persistent associative memory that survives between Director cycles.
Runs as a local HTTP server on localhost:19876.
Maintains in-memory state: concept associations with weights, active focus, running experiments.

API:
  POST /add         {"concept": "...", "associations": [{"target": "...", "weight": 0.8}, ...]}
  POST /query       {"concept": "...", "limit": 10}  → related concepts sorted by weight
  POST /dump        {}  → full memory dump
  POST /focus       {"concept": "..."}  → set active focus
  POST /status      {}  → uptime, memory size, focus
  POST /decay       {"rate": 0.01}  → decay all weights slightly (forgetting)
  POST /reset       {}  → clear everything
"""

import json
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

"""Working Memory Daemon — main entry point.

Run: python3 working_memory_daemon.py
"""


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads."""
    daemon_threads = True
from collections import defaultdict
from datetime import datetime

PORT = 19876
STATE_FILE = os.path.expanduser("~/.hermes/working_memory_state.json")

class WorkingMemory:
    """In-memory associative network with persistence."""
    
    def __init__(self):
        self.associations = defaultdict(dict)  # concept → {target: weight}
        self.focus = None
        self.cycles_seen = 0
        self.started_at = time.time()
        self.last_activity = time.time()
        self.total_operations = 0
        self._lock = threading.RLock()
        self._load()
    
    def add(self, concept, associations):
        """Add or update associations for a concept."""
        with self._lock:
            for assoc in associations:
                target = assoc["target"]
                weight = float(assoc.get("weight", 0.5))
                # Exponential moving average for weight updates
                if target in self.associations[concept]:
                    old = self.associations[concept][target]
                    self.associations[concept][target] = 0.7 * old + 0.3 * weight
                else:
                    self.associations[concept][target] = weight
                # Also add reverse association (weaker)
                if concept in self.associations[target]:
                    self.associations[target][concept] = max(
                        self.associations[target][concept],
                        weight * 0.7
                    )
                else:
                    self.associations[target][concept] = weight * 0.7
            
            self.cycles_seen += 1
            self.last_activity = time.time()
            self.total_operations += 1
            self._save()
    
    def query(self, concept, limit=10):
        """Get top-N related concepts by weight."""
        with self._lock:
            self.total_operations += 1
            self.last_activity = time.time()
            if concept not in self.associations:
                return []
            related = sorted(
                self.associations[concept].items(),
                key=lambda x: x[1],
                reverse=True
            )[:limit]
            return [{"concept": k, "weight": round(v, 4)} for k, v in related]
    
    def set_focus(self, concept):
        """Set the active focus concept."""
        with self._lock:
            old_focus = self.focus
            self.focus = concept
            self.last_activity = time.time()
            self.total_operations += 1
            self._save()
            return {"previous": old_focus, "current": concept}
    
    def decay(self, rate=0.01):
        """Decay all association weights slightly."""
        with self._lock:
            decayed = 0
            for concept in list(self.associations.keys()):
                for target in list(self.associations[concept].keys()):
                    self.associations[concept][target] *= (1.0 - rate)
                    if self.associations[concept][target] < 0.01:
                        del self.associations[concept][target]
                        decayed += 1
                if not self.associations[concept]:
                    del self.associations[concept]
            self.last_activity = time.time()
            self._save()
            return {"decayed": decayed, "rate": rate}
    
    def dump(self):
        """Full dump of working memory."""
        with self._lock:
            concepts = {}
            for concept, targets in self.associations.items():
                concepts[concept] = [
                    {"target": t, "weight": round(w, 4)}
                    for t, w in sorted(targets.items(), key=lambda x: x[1], reverse=True)
                ]
            return {
                "focus": self.focus,
                "concepts": concepts,
                "cycles_seen": self.cycles_seen,
                "uptime_seconds": time.time() - self.started_at,
                "total_operations": self.total_operations,
            }
    
    def status(self):
        """Quick status."""
        with self._lock:
            self.total_operations += 1
            self.last_activity = time.time()
            return {
                "focus": self.focus,
                "total_concepts": len(self.associations),
                "total_associations": sum(len(t) for t in self.associations.values()),
                "cycles_seen": self.cycles_seen,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "total_operations": self.total_operations,
            }
    
    def _save(self):
        """Persist to disk as backup — runs in caller's thread but catches all errors."""
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            with open(STATE_FILE, 'w') as f:
                json.dump(self.dump(), f, indent=2)
        except Exception:
            pass  # persistence is best-effort, never block on it
    
    def _load(self):
        """Restore from disk if available."""
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
                for concept, targets in data.get("concepts", {}).items():
                    for t in targets:
                        self.associations[concept][t["target"]] = t["weight"]
                self.focus = data.get("focus")
                self.total_operations = data.get("total_operations", 0)
                self.cycles_seen = data.get("cycles_seen", 0)
        except (FileNotFoundError, json.JSONDecodeError):
            pass


wm = WorkingMemory()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length else b'{}'
            data = json.loads(body)
        except json.JSONDecodeError:
            self._respond(400, {"error": "invalid json"})
            return
        except Exception as e:
            self._respond(500, {"error": f"read error: {str(e)}"})
            return
        
        try:
            path = self.path.rstrip('/')
            
            if path == '/add':
                wm.add(data.get("concept", ""), data.get("associations", []))
                self._respond(200, {"status": "ok", "cycles": wm.cycles_seen})
            elif path == '/query':
                result = wm.query(data.get("concept", ""), data.get("limit", 10))
                self._respond(200, {"results": result})
            elif path == '/dump':
                self._respond(200, wm.dump())
            elif path == '/focus':
                result = wm.set_focus(data.get("concept", ""))
                self._respond(200, result)
            elif path == '/status':
                self._respond(200, wm.status())
            elif path == '/decay':
                result = wm.decay(data.get("rate", 0.01))
                self._respond(200, result)
            elif path == '/reset':
                wm.__init__()
                self._respond(200, {"status": "reset"})
            else:
                self._respond(404, {"error": "unknown endpoint", "path": path})
        except Exception as e:
            self._respond(500, {"error": str(e)})
    
    def _respond(self, code, data):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def log_message(self, format, *args):
        pass  # silent


def main():
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print(f"Working memory daemon started on port {PORT}")
    print(f"PID: {os.getpid()}")
    print(f"State file: {STATE_FILE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        wm._save()
        server.shutdown()


if __name__ == "__main__":
    main()
