from email import policy
from email.parser import BytesParser
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
from uuid import uuid4
import os

BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "Scripts"
RAW_REPLAYS_DIR = BASE_DIR / "Replay Data" / "Raw Replays"
HTML_DIR = BASE_DIR / "Replay Data" / "Replay HTMLs"
FRONTEND_DIR = BASE_DIR / "frontend"
EXAMPLE_HTML = BASE_DIR / "examples" / "example-replay.html"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def safe_stem(filename):
    stem = Path(filename).stem
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return cleaned or "replay"


def json_response(handler, status, payload):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class ReplayServer(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_file(FRONTEND_DIR / "index.html", "text/html; charset=utf-8")
            return

        if parsed.path == "/example":
            self.serve_file(EXAMPLE_HTML, "text/html; charset=utf-8")
            return

        if parsed.path.startswith("/simulation/"):
            filename = Path(unquote(parsed.path.removeprefix("/simulation/"))).name
            html_path = (HTML_DIR / filename).resolve()
            if html_path.parent != HTML_DIR.resolve() or not html_path.exists():
                json_response(self, 404, {"error": "Simulation not found."})
                return
            self.serve_file(html_path, "text/html; charset=utf-8")
            return

        json_response(self, 404, {"error": "Not found."})

    def do_POST(self):
        if urlparse(self.path).path != "/api/upload":
            json_response(self, 404, {"error": "Not found."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0

        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            json_response(self, 413, {"error": "Upload must be between 1 byte and 500 MB."})
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            json_response(self, 400, {"error": "Upload must use multipart/form-data."})
            return

        body = self.rfile.read(content_length)
        message = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
            + body
        )
        upload = next(
            (
                part
                for part in message.iter_attachments()
                if part.get_param("name", header="Content-Disposition") == "replay"
            ),
            None,
        )
        if upload is None:
            json_response(self, 400, {"error": "Choose a .replay file to upload."})
            return

        original_name = upload.get_filename() or "replay.replay"
        if Path(original_name).suffix.lower() != ".replay":
            json_response(self, 400, {"error": "Only .replay files are supported."})
            return

        replay_name = f"{safe_stem(original_name)}-{uuid4().hex[:8]}.replay"
        html_name = f"{Path(replay_name).stem}.html"
        replay_path = RAW_REPLAYS_DIR / replay_name
        html_path = HTML_DIR / html_name
        RAW_REPLAYS_DIR.mkdir(parents=True, exist_ok=True)
        HTML_DIR.mkdir(parents=True, exist_ok=True)
        replay_path.write_bytes(upload.get_payload(decode=True) or b"")

        command = [
            sys.executable,
            str(SCRIPTS_DIR / "run_pipeline.py"),
            "-i",
            str(replay_path),
            "-o",
            str(html_path),
        ]
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0 or not html_path.exists():
            replay_path.unlink(missing_ok=True)
            json_response(
                self,
                422,
                {
                    "error": "The replay could not be processed.",
                    "details": (result.stderr or result.stdout).strip()[-4000:],
                },
            )
            return

        json_response(
            self,
            201,
            {"filename": html_name, "simulationUrl": f"/simulation/{html_name}"},
        )

    def serve_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            json_response(self, 404, {"error": "Not found."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        print(f"[{self.log_date_time_string}] {format_string % args}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    server = ThreadingHTTPServer(("0.0.0.0", port), ReplayServer)
    print(f"Rocket League Replay Analytics running on port {port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()