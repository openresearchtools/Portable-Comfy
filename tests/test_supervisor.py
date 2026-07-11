from __future__ import annotations

import json
import urllib.request

import pytest

from portable_comfy.paths import PortablePaths
from portable_comfy.supervisor import ServerStartError, ServerSupervisor


SERVER = r"""
import argparse, http.server, json, signal, threading
p=argparse.ArgumentParser(add_help=False)
p.add_argument('--port', type=int, required=True)
args,_=p.parse_known_args()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/system_stats':
            data=json.dumps({'system': {'test': True}}).encode()
            self.send_response(200); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
        elif self.path == '/':
            data=b'<!doctype html><title>ComfyUI test</title>'
            self.send_response(200); self.send_header('Content-Length',str(len(data)))
            self.end_headers(); self.wfile.write(data)
        else: self.send_error(404)
    def log_message(self,*a): pass
s=http.server.ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
def stop(*a): threading.Thread(target=s.shutdown,daemon=True).start()
signal.signal(signal.SIGINT,stop); signal.signal(signal.SIGTERM,stop)
print('READY', flush=True); s.serve_forever()
"""


def test_supervisor_start_health_and_stop(portable_root: PortablePaths) -> None:
    (portable_root.comfyui / "main.py").write_text(SERVER, encoding="utf-8")
    supervisor = ServerSupervisor(portable_root, start_timeout=10)
    url = supervisor.start()
    assert url.startswith("http://127.0.0.1:")
    assert supervisor.is_running
    state = json.loads((portable_root.state / "server.json").read_text())
    assert state["pid"] == supervisor.status().pid
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url + "system_stats", timeout=2) as response:
        assert response.status == 200
    supervisor.stop(interrupt_timeout=3, terminate_timeout=1)
    assert not supervisor.is_running
    assert not (portable_root.state / "server.json").exists()


def test_supervisor_reports_early_exit(portable_root: PortablePaths) -> None:
    (portable_root.comfyui / "main.py").write_text(
        "import sys; print('intentional failure', flush=True); sys.exit(7)\n",
        encoding="utf-8",
    )
    supervisor = ServerSupervisor(portable_root, start_timeout=3)
    with pytest.raises(ServerStartError, match="status 7") as failure:
        supervisor.start()
    assert "intentional failure" in str(failure.value)
    assert not supervisor.is_running


def test_supervisor_timeout_terminates_owned_group(
    portable_root: PortablePaths,
) -> None:
    (portable_root.comfyui / "main.py").write_text(
        "import time; print('never healthy', flush=True); time.sleep(30)\n",
        encoding="utf-8",
    )
    supervisor = ServerSupervisor(
        portable_root, start_timeout=0.5, health_probe=lambda _port, _timeout: False
    )
    with pytest.raises(ServerStartError, match="healthy"):
        supervisor.start()
    assert not supervisor.is_running
    assert "never healthy" in supervisor.tail_log()
