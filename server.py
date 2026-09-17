from email import policy
from email.parser import BytesParser
import json
import logging
import platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote, urlparse
from uuid import uuid4
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "Scripts"
RAW_REPLAYS_DIR = BASE_DIR / "Replay Data" / "Raw Replays"
HTML_DIR = BASE_DIR / "Replay Data" / "Replay HTMLs"
FRONTEND_DIR = BASE_DIR / "frontend"
EXAMPLE_HTML = BASE_DIR / "examples" / "example-replay.html"
PARSED_CSV_DIR = BASE_DIR / "Replay Data" / "Parsed CSVs"
LOG_DIR = BASE_DIR / "logs"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_QUESTION_BYTES = 16 * 1024

EXAMPLE_STEM = "265a9dbc-6e9f-471e-bd3a-23cfc6ee8a10"

# --- Logging setup -----------------------------------------------------
# Writes to both stdout (so `docker compose logs -f` shows it live) and a
# file under ./logs (mount this as a volume to persist/inspect it outside
# the container). Level can be overridden per-device via LOG_LEVEL env var,
# handy when debugging something that only reproduces on one machine.
LOG_DIR.mkdir(exist_ok=True)
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("rl-analytics")


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
        logger.info(f"GET {parsed.path} from {self.client_address[0]}")

        if parsed.path == "/":
            self.serve_file(FRONTEND_DIR / "index.html", "text/html; charset=utf-8")
            return

        if parsed.path == "/example":
            self.serve_file(EXAMPLE_HTML, "text/html; charset=utf-8")
            return

        if parsed.path == "/api/example":
            json_response(self, 200, {
                "stem": EXAMPLE_STEM,
                "simulationUrl": "/example",
                "askable": (PARSED_CSV_DIR / f"{EXAMPLE_STEM}.csv").exists(),
            })
            return

        if parsed.path.startswith("/simulation/"):
            filename = Path(unquote(parsed.path.removeprefix("/simulation/"))).name
            html_path = (HTML_DIR / filename).resolve()
            if html_path.parent != HTML_DIR.resolve() or not html_path.exists():
                logger.warning(f"Simulation not found: {filename}")
                json_response(self, 404, {"error": "Simulation not found."})
                return
            self.serve_file(html_path, "text/html; charset=utf-8")
            return

        logger.warning(f"404 for GET {parsed.path}")
        json_response(self, 404, {"error": "Not found."})

    def do_POST(self):
        path = urlparse(self.path).path
        logger.info(f"POST {path} from {self.client_address[0]}")

        if path == "/api/ask":
            self.handle_ask()
            return
        if path != "/api/upload":
            logger.warning(f"404 for POST {path}")
            json_response(self, 404, {"error": "Not found."})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0

        logger.info(f"Upload request: {content_length} bytes")

        if content_length <= 0 or content_length > MAX_UPLOAD_BYTES:
            logger.warning(f"Rejected upload: bad size {content_length}")
            json_response(self, 413, {"error": "Upload must be between 1 byte and 500 MB."})
            return

        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            logger.warning(f"Rejected upload: bad content-type {content_type!r}")
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
            logger.warning("Rejected upload: no 'replay' field found in form data")
            json_response(self, 400, {"error": "Choose a .replay file to upload."})
            return

        original_name = upload.get_filename() or "replay.replay"
        if Path(original_name).suffix.lower() != ".replay":
            logger.warning(f"Rejected upload: bad extension on {original_name!r}")
            json_response(self, 400, {"error": "Only .replay files are supported."})
            return

        replay_name = f"{safe_stem(original_name)}-{uuid4().hex[:8]}.replay"
        html_name = f"{Path(replay_name).stem}.html"
        replay_path = RAW_REPLAYS_DIR / replay_name
        html_path = HTML_DIR / html_name
        RAW_REPLAYS_DIR.mkdir(parents=True, exist_ok=True)
        HTML_DIR.mkdir(parents=True, exist_ok=True)
        replay_path.write_bytes(upload.get_payload(decode=True) or b"")

        logger.info(f"Saved upload as {replay_path}, running pipeline -> {html_path}")

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
            logger.error(
                f"Pipeline failed (exit {result.returncode}) for {replay_path}: "
                f"{(result.stderr or result.stdout).strip()[-2000:]}"
            )
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

        logger.info(f"Pipeline succeeded: {html_name}")
        json_response(
            self,
            201,
            {
                "filename": html_name,
                "stem": Path(replay_name).stem,
                "simulationUrl": f"/simulation/{html_name}",
            },
        )

    def handle_ask(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0

        if content_length <= 0 or content_length > MAX_QUESTION_BYTES:
            json_response(self, 400, {"error": "Ask a question of reasonable length."})
            return

        try:
            request = json.loads(self.rfile.read(content_length))
        except (ValueError, UnicodeDecodeError):
            json_response(self, 400, {"error": "Expected a JSON body."})
            return

        question = str(request.get("question") or "").strip()
        if not question:
            json_response(self, 400, {"error": "Ask a question first."})
            return

        stem = Path(str(request.get("stem") or "")).name
        logger.info(f"Ask request: stem={stem!r} question={question!r}")

        csv_path = (PARSED_CSV_DIR / f"{stem}.csv").resolve()
        if csv_path.parent != PARSED_CSV_DIR.resolve() or not csv_path.exists():
            logger.warning(f"Ask rejected: no CSV for stem {stem!r} at {csv_path}")
            json_response(self, 404, {
                "error": "That replay's data is no longer available. Upload it again."
            })
            return

        if not os.environ.get("GEMINI_API_KEY"):
            logger.warning("Ask rejected: GEMINI_API_KEY not set in environment")
            json_response(self, 503, {
                "error": "AI coaching is off. Set GEMINI_API_KEY and restart the server."
            })
            return

        try:
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            import coach_agent
        except ImportError as error:
            logger.error(f"coach_agent import failed: {error}")
            json_response(self, 503, {
                "error": "AI coaching is not installed.",
                "details": f"{error}. Run: pip install -r requirements.txt",
            })
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def emit(event):
            self.wfile.write((json.dumps(event) + "\n").encode("utf-8"))
            self.wfile.flush()

        try:
            for event in coach_agent.answer_question(
                stem, question, request.get("player")
            ):
                emit(event)
            logger.info(f"Ask completed for stem={stem!r}")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.info(f"Ask connection dropped by client for stem={stem!r}")
            return
        except Exception as error:
            logger.exception(f"Ask failed for stem={stem!r}")
            try:
                emit({"type": "error", "error": f"{type(error).__name__}: {error}"})
            except OSError:
                pass

    def serve_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            logger.warning(f"File not found: {path}")
            json_response(self, 404, {"error": "Not found."})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string, *args):
        # Routed through our logger instead of raw print, and fixed the
        # missing () on log_date_time_string that used to print a method
        # object instead of a timestamp.
        logger.info(f"{self.log_date_time_string()} {format_string % args}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))

    # Environment/diagnostic dump — printed once at startup, extremely
    # useful when something works on one machine/container but not another.
    logger.info("=" * 60)
    logger.info("Rocket League Replay Analytics starting")
    logger.info(f"Python: {sys.version}")
    logger.info(f"Platform: {platform.platform()}")
    logger.info(f"BASE_DIR: {BASE_DIR}")
    logger.info(f"PORT: {port}")
    logger.info(f"GEMINI_API_KEY set: {bool(os.environ.get('GEMINI_API_KEY'))}")
    logger.info(f"LOG_LEVEL: {log_level}")
    logger.info(f"Running in Docker: {Path('/.dockerenv').exists()}")
    logger.info("=" * 60)

    server = ThreadingHTTPServer(("0.0.0.0", port), ReplayServer)
    logger.info(f"Listening on 0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    finally:
        server.server_close()