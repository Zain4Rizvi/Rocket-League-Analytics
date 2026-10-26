from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
import json
import logging
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time

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
METADATA_DIR = BASE_DIR / "Replay Data" / "Game Metadata"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "Replay Data" / "app.sqlite"
MAX_UPLOAD_BYTES = 500 * 1024 * 1024
MAX_QUESTION_BYTES = 16 * 1024
MAX_AUTH_BYTES = 16 * 1024
SESSION_COOKIE = "rl_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
_DB_LOCK = threading.RLock()
_DB_READY = False

EXAMPLE_STEM = "example-replay"

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


def json_response(handler, status, payload, extra_headers=None):
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    for name, value in extra_headers or ():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def google_client_id():
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def db_connect(init=True):
    if init:
        init_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db():
    global _DB_READY
    with _DB_LOCK:
        if _DB_READY:
            return
        conn = db_connect(init=False)
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    google_sub TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(google_sub)
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    stem TEXT NOT NULL,
                    match_date TEXT,
                    playlist TEXT,
                    uploaded_at INTEGER NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(google_sub)
                );
                CREATE INDEX IF NOT EXISTS history_user_uploaded
                    ON history(user_id, uploaded_at DESC);
                """
            )
            conn.commit()
            _DB_READY = True
        finally:
            conn.close()


def playlist_from_metadata(meta):
    bots = {
        player.get("name")
        for player in (meta.get("player_stats") or [])
        if player.get("bBot")
    }
    def team_size(names):
        return sum(1 for name in (names or []) if name not in bots)

    size = max(team_size(meta.get("BLUE_TEAM")), team_size(meta.get("ORANGE_TEAM")), 1)
    return f"{size}s"


def history_fields_from_stem(stem):
    path = METADATA_DIR / f"{stem}.json"
    if not path.exists():
        return None, None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    return meta.get("date"), playlist_from_metadata(meta)


def same_origin(handler):
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    return urlparse(origin).netloc == handler.headers.get("Host", "")


def session_cookie_header(token, clear=False):
    # SameSite=None; Secure so the GIS callback fetch (cross-site initiator)
    # can still store the session. localhost is a secure context in Chromium.
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE] = "" if clear else token
    morsel = cookie[SESSION_COOKIE]
    morsel["path"] = "/"
    morsel["httponly"] = True
    morsel["samesite"] = "None"
    morsel["secure"] = True
    morsel["max-age"] = "0" if clear else str(SESSION_SECONDS)
    return ("Set-Cookie", morsel.OutputString())


def session_token_from_request(handler):
    auth = handler.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            return token
    raw = handler.headers.get("Cookie") or ""
    # Don't use SimpleCookie.load: Google's g_state value is JSON and
    # makes the parser drop every cookie on the header, including ours.
    match = re.search(rf"(?:^|;\s*){re.escape(SESSION_COOKIE)}=([^;]*)", raw)
    if not match:
        return None
    return match.group(1).strip() or None


def current_user(handler):
    token = session_token_from_request(handler)
    if not token:
        return None
    now = int(time.time())
    with _DB_LOCK:
        conn = db_connect()
        try:
            row = conn.execute(
                """
                SELECT u.google_sub, u.email, u.name
                FROM sessions s
                JOIN users u ON u.google_sub = s.user_id
                WHERE s.token = ? AND s.expires_at > ?
                """,
                (token, now),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def upsert_user_and_session(profile):
    token = secrets.token_urlsafe(32)
    expires_at = int(time.time()) + SESSION_SECONDS
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO users (google_sub, email, name) VALUES (?, ?, ?)
                ON CONFLICT(google_sub) DO UPDATE SET email = excluded.email, name = excluded.name
                """,
                (profile["google_sub"], profile["email"], profile["name"]),
            )
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, profile["google_sub"], expires_at),
            )
            conn.commit()
        finally:
            conn.close()
    return token


def delete_session(token):
    if not token:
        return
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()


def insert_history(user_id, stem, match_date, playlist):
    with _DB_LOCK:
        conn = db_connect()
        try:
            conn.execute(
                """
                INSERT INTO history (user_id, stem, match_date, playlist, uploaded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, stem, match_date, playlist, int(time.time())),
            )
            conn.commit()
        finally:
            conn.close()


def list_history(user_id):
    with _DB_LOCK:
        conn = db_connect()
        try:
            rows = conn.execute(
                """
                SELECT stem, match_date, playlist, uploaded_at
                FROM history
                WHERE user_id = ?
                ORDER BY uploaded_at DESC, id DESC
                """,
                (user_id,),
            ).fetchall()
        finally:
            conn.close()
    items = []
    for row in rows:
        stem = row["stem"]
        items.append({
            "stem": stem,
            "matchDate": row["match_date"],
            "playlist": row["playlist"],
            "uploadedAt": row["uploaded_at"],
            "simulationUrl": f"/simulation/{stem}.html",
        })
    return items


def verify_google_id_token(credential):
    client_id = google_client_id()
    if not client_id or not credential:
        return None
    url = "https://oauth2.googleapis.com/tokeninfo?" + urlencode({"id_token": credential})
    try:
        request = Request(url, headers={"User-Agent": "rl-analytics/1.0"})
        with urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, HTTPError, TimeoutError, ValueError, OSError):
        return None
    if data.get("aud") != client_id:
        return None
    if data.get("iss") not in GOOGLE_ISSUERS:
        return None
    if str(data.get("email_verified", "")).lower() not in {"true", "1"}:
        return None
    try:
        exp = int(data.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if exp <= int(time.time()):
        return None
    sub = data.get("sub")
    email = data.get("email")
    if not sub or not email:
        return None
    return {
        "google_sub": sub,
        "email": email,
        "name": data.get("name") or data.get("given_name") or email,
    }


def read_json_body(handler, max_bytes):
    try:
        content_length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        content_length = 0
    if content_length <= 0 or content_length > max_bytes:
        return None, "Request body is missing or too large."
    try:
        return json.loads(handler.rfile.read(content_length)), None
    except (ValueError, UnicodeDecodeError):
        return None, "Expected a JSON body."


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

        if parsed.path == "/api/config":
            json_response(self, 200, {"googleClientId": google_client_id() or None})
            return

        if parsed.path == "/api/me":
            user = current_user(self)
            if not user:
                json_response(self, 401, {"error": "Not signed in."})
                return
            json_response(self, 200, {"email": user["email"], "name": user["name"]})
            return

        if parsed.path == "/api/history":
            user = current_user(self)
            if not user:
                json_response(self, 401, {"error": "Not signed in."})
                return
            json_response(self, 200, {"items": list_history(user["google_sub"])})
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
        if path == "/api/auth/google":
            self.handle_google_auth()
            return
        if path == "/api/auth/logout":
            self.handle_logout()
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
        stem = Path(replay_name).stem
        match_date, playlist = history_fields_from_stem(stem)
        uploaded_at = int(time.time())
        payload = {
            "filename": html_name,
            "stem": stem,
            "simulationUrl": f"/simulation/{html_name}",
            "matchDate": match_date,
            "playlist": playlist,
            "uploadedAt": uploaded_at,
        }
        user = current_user(self)
        if user:
            insert_history(user["google_sub"], stem, match_date, playlist)
        json_response(self, 201, payload)

    def handle_google_auth(self):
        if not same_origin(self):
            json_response(self, 403, {"error": "Bad origin."})
            return
        if not google_client_id():
            json_response(self, 503, {"error": "Google sign-in is not configured."})
            return
        body, error = read_json_body(self, MAX_AUTH_BYTES)
        if error:
            json_response(self, 400, {"error": error})
            return
        credential = str((body or {}).get("credential") or "").strip()
        profile = verify_google_id_token(credential)
        if not profile:
            json_response(self, 401, {"error": "Google sign-in could not be verified."})
            return
        token = upsert_user_and_session(profile)
        json_response(
            self,
            200,
            {"email": profile["email"], "name": profile["name"], "session": token},
            extra_headers=[session_cookie_header(token)],
        )

    def handle_logout(self):
        if not same_origin(self):
            json_response(self, 403, {"error": "Bad origin."})
            return
        token = session_token_from_request(self)
        delete_session(token)
        json_response(
            self,
            200,
            {"ok": True},
            extra_headers=[session_cookie_header("", clear=True)],
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
        if path.name == "index.html":
            self.send_header("Cache-Control", "no-store")
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
    logger.info(f"GOOGLE_CLIENT_ID set: {bool(google_client_id())}")
    logger.info(f"LOG_LEVEL: {log_level}")
    logger.info(f"Running in Docker: {Path('/.dockerenv').exists()}")
    logger.info("=" * 60)

    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", port), ReplayServer)
    logger.info(f"Listening on 0.0.0.0:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down (KeyboardInterrupt)")
    finally:
        server.server_close()