"""Run the served helper scripts against a local HTTP stub with real curl."""

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FAKE_KEY = "-".join(["test", "only"])

pytestmark = pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("curl") and shutil.which("jq")),
    reason="needs bash, curl and jq",
)


@pytest.fixture
def hub():
    """A stub hub that records each request and answers like POST /{lane}/sendFile."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
            out = json.dumps({"ok": True, "messageId": 7, "chatId": "-100"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def _project(tmp_path, base, chat_id=""):
    shutil.copy(SCRIPTS / "tg-send-file.sh", tmp_path / "tg-send-file.sh")
    env_lines = [f"LANEHUB_BASE={base}", "LANEHUB_LANE=backend", f"LANEHUB_API_KEY={FAKE_KEY}"]
    if chat_id:
        env_lines.append(f"LANEHUB_CHAT_ID={chat_id}")
    (tmp_path / ".lanehub.env").write_text("\n".join(env_lines) + "\n")
    (tmp_path / "shot.png").write_bytes(b"\x89PNG fake")
    return tmp_path


def _field(body: bytes, name: str) -> str:
    marker = f'name="{name}"\r\n\r\n'.encode()
    assert marker in body, f"no {name} field in the upload"
    return body.split(marker, 1)[1].split(b"\r\n--", 1)[0].decode()


@pytest.mark.parametrize("caption", [
    "@IrishkaSit Ира, глянь превью",   # a mention first: -F read it as a file and curl exited 26
    "<not-a-file> still text",          # -F reads "<path" as file contents
    "plain caption",
])
def test_send_file_caption_is_sent_verbatim(tmp_path, hub, caption):
    base, seen = hub
    proj = _project(tmp_path, base, chat_id="-100")
    res = subprocess.run(["bash", "tg-send-file.sh", "shot.png", caption],
                         cwd=proj, capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "sent ✓ shot.png" in res.stdout
    assert len(seen) == 1
    req = seen[0]
    assert req["path"] == "/backend/sendFile"
    assert req["headers"]["X-Bridge-Token"] == FAKE_KEY
    assert _field(req["body"], "caption") == caption
    assert _field(req["body"], "asDocument") == "false"
    assert _field(req["body"], "chatId") == "-100"
    assert b"\x89PNG fake" in req["body"]
    logged = json.loads((proj / "tg-chat-log.jsonl").read_text().splitlines()[-1])
    assert logged["text"] == caption and logged["ok"] is True and logged["file"] == "shot.png"


def test_send_file_without_caption_sends_no_caption_field(tmp_path, hub):
    base, seen = hub
    proj = _project(tmp_path, base)
    res = subprocess.run(["bash", "tg-send-file.sh", "--doc", "shot.png"],
                         cwd=proj, capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stdout + res.stderr
    body = seen[0]["body"]
    assert b'name="caption"' not in body and b'name="chatId"' not in body
    assert _field(body, "asDocument") == "true"
