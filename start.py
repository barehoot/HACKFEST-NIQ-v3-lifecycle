"""
start.py

One command to set up and run Talent 360i locally (macOS, Linux, Windows):

    python start.py              # first run: configure, build data, start both servers
    python start.py --setup      # configure + build data only, don't start servers
    python start.py --reseed     # re-apply the passwords in .env to every login account
    python start.py --rebuild    # rebuild reference data (roles/skills/training) from the dataset
    python start.py --reload     # dev mode: auto-restart a server when its own code changes
    python start.py --no-worker  # skip the lifecycle worker (probation tracking, reminders, status refresh)

What it does, in order:
  1. Checks the Python version and that requirements.txt is installed.
  2. Creates .env (project root) and the portal's .env from their .env.example
     files if missing, and fills in any EMPTY secret with a random value
     written back into .env (never printed, never hardcoded): the shared
     portal->API bearer token, the portal session secret, and the demo /
     synthetic login passwords. Values you already set are never changed.
  3. Builds db/talent360i.sqlite + db/chroma_store from data/*.xlsx if they
     don't exist yet, and seeds the login accounts on first run.
  4. Starts the pipeline API (127.0.0.1:8000), the portal (127.0.0.1:8010) and
     the lifecycle worker as child processes, waits until the servers answer,
     and stops all of them on Ctrl+C or as soon as one exits.

Ports can be changed with TALENT360_API_PORT / TALENT360_PORTAL_PORT.
"""

import argparse
import importlib.util
import os
import re
import secrets
import signal
import socket
import sqlite3
import string
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PORTAL_DIR = ROOT / "Talent360i_phase11C_fixed_remote_hybrid"
ROOT_ENV, PORTAL_ENV = ROOT / ".env", PORTAL_DIR / ".env"
DB_PATH = ROOT / "db" / "talent360i.sqlite"
CHROMA_PATH = ROOT / "db" / "chroma_store"

HOST = "127.0.0.1"  # loopback only -- nothing is exposed to the network
API_PORT = int(os.getenv("TALENT360_API_PORT", "8000"))
PORTAL_PORT = int(os.getenv("TALENT360_PORTAL_PORT", "8010"))

DEMO_ACCOUNTS = ["employee", "manager", "sme", "scheduler", "leader", "admin"]


def say(msg=""):
    print(msg, flush=True)


def fail(msg):
    say(f"\nERROR: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 1. Environment checks
# ---------------------------------------------------------------------------

def check_python():
    if sys.version_info < (3, 12):
        fail(
            f"Python 3.12 is required (this is {sys.version.split()[0]}).\n"
            "  Create the virtualenv with Python 3.12, e.g.:\n"
            "    python3.12 -m venv .venv      (Windows: py -3.12 -m venv .venv)"
        )


def check_dependencies():
    modules = ("fastapi", "uvicorn", "jinja2", "itsdangerous", "bcrypt", "dotenv", "chromadb", "pandas", "openpyxl")
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if missing:
        fail(
            f"Missing packages: {', '.join(missing)}.\n"
            "  Activate the virtualenv and run:  pip install -r requirements.txt"
        )


# ---------------------------------------------------------------------------
# 2. .env files
# ---------------------------------------------------------------------------

def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def set_env_values(path, updates):
    """Rewrites only the given KEY= lines (appending missing keys), keeping every comment."""
    lines = path.read_text(encoding="utf-8").splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match.group(1) in pending:
            key = match.group(1)
            lines[i] = f"{key}={pending.pop(key)}"
    lines += [f"{key}={value}" for key, value in pending.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def random_password():
    # 20 chars, always lower+upper+digit+symbol -- passes services/security.validate_password.
    alphabet = string.ascii_letters + string.digits + "-_!@#%^*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(20))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "-_!@#%^*" for c in pw)):
            return pw


def ensure_env_files():
    for env, example in ((ROOT_ENV, ROOT / ".env.example"), (PORTAL_ENV, PORTAL_DIR / ".env.example")):
        if not env.exists():
            env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            say(f"  created {env.relative_to(ROOT)} from {example.name}")

    root = read_env(ROOT_ENV)
    generated = {}
    if not root.get("TALENT360_BACKEND_TOKEN"):
        generated["TALENT360_BACKEND_TOKEN"] = secrets.token_urlsafe(32)
    for key in ("TALENT360_DEMO_PASSWORD", "TALENT360_SYNTHETIC_PASSWORD"):
        if not root.get(key):
            generated[key] = random_password()
    if generated:
        set_env_values(ROOT_ENV, generated)
        say(f"  generated {', '.join(generated)} -> stored in .env")
        root.update(generated)

    portal = read_env(PORTAL_ENV)
    portal_updates = {}
    # The portal must present exactly the token the API expects.
    if portal.get("TALENT360_BACKEND_TOKEN") != root["TALENT360_BACKEND_TOKEN"]:
        portal_updates["TALENT360_BACKEND_TOKEN"] = root["TALENT360_BACKEND_TOKEN"]
    if not portal.get("TALENT360_SESSION_SECRET"):
        portal_updates["TALENT360_SESSION_SECRET"] = secrets.token_urlsafe(48)
    if portal.get("TALENT360_API_MODE", "").lower() != "remote":
        portal_updates["TALENT360_API_MODE"] = "remote"
    if portal_updates:
        set_env_values(PORTAL_ENV, portal_updates)
        say(f"  set {', '.join(portal_updates)} in {PORTAL_ENV.relative_to(ROOT)}")
    return root


def check_llm_key(root_env):
    provider = (root_env.get("LLM_PROVIDER") or "groq").lower()
    key_name = {"groq": "GROQ_API_KEY", "cis": "CIS_API_KEY"}.get(provider)
    if key_name is None:
        fail(f"LLM_PROVIDER='{provider}' in .env is not supported -- use 'groq' or 'cis'.")
    if len(root_env.get(key_name, "")) < 8:
        say(
            f"\n  WARNING: {key_name} is empty in .env (LLM_PROVIDER={provider}).\n"
            "  The portal will run, but the AI steps (question generation, case studies,\n"
            "  evidence report, development plan) will fail until you add the key and restart."
        )
        return False
    say(f"  LLM provider: {provider} ({key_name} is set)")
    return True


# ---------------------------------------------------------------------------
# 3. Data
# ---------------------------------------------------------------------------

def run_script(name):
    say(f"  running scripts/{name} ...")
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / name)], cwd=ROOT,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        say(result.stdout[-3000:])
        say(result.stderr[-3000:])
        fail(f"scripts/{name} failed (exit code {result.returncode}).")


def table_rows(table):
    if not DB_PATH.exists():
        return 0
    conn = sqlite3.connect(DB_PATH)
    try:
        # Fixed table names only (bandit B608 reviewed).
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # nosec B608
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


DEMO_USERNAMES = ("employee", "manager", "manager2", "manager3", "sme", "scheduler", "leader", "admin", "hr")


def missing_demo_accounts():
    if not DB_PATH.exists():
        return True
    conn = sqlite3.connect(DB_PATH)
    try:
        found = {r[0] for r in conn.execute("SELECT username FROM users")}
        return not set(DEMO_USERNAMES) <= found
    except sqlite3.Error:
        return True
    finally:
        conn.close()


def ensure_data(rebuild=False, reseed=False):
    if rebuild or table_rows("role_master") == 0 or not CHROMA_PATH.exists():
        say("  building reference data from data/*.xlsx (first run takes a minute;")
        say("  build_chroma downloads a small ONNX embedding model once) ...")
        run_script("build_sqlite.py")
        run_script("build_chroma.py")
        run_script("seed_r2r_training_catalogue.py")
    if reseed or table_rows("users") == 0 or missing_demo_accounts():
        run_script("seed_users.py")  # also adds demo accounts introduced by a newer build (hr, manager2, manager3)
    run_script("seed_question_bank.py")  # idempotent: dataset questions -> reusable bank
    run_script("seed_lifecycle.py")      # idempotent: v3 lifecycle demo data + status snapshot
    say(f"  database ready: {DB_PATH.relative_to(ROOT)} ({table_rows('users')} login accounts)")


# ---------------------------------------------------------------------------
# 4. Servers
# ---------------------------------------------------------------------------

def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((HOST, port)) != 0


def wait_until_up(url, proc, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            # Fixed http://127.0.0.1 URL (bandit B310 reviewed).
            with urllib.request.urlopen(url, timeout=2):  # nosec B310
                return True
        except OSError:
            time.sleep(0.5)
    return False


def start_servers(reload=False, worker=True):
    for port, what, var in ((API_PORT, "pipeline API", "TALENT360_API_PORT"),
                            (PORTAL_PORT, "portal", "TALENT360_PORTAL_PORT")):
        if not port_free(port):
            fail(f"Port {port} ({what}) is already in use. Stop the other process "
                 f"(an old uvicorn?) or set {var} to a free port.")

    env = dict(os.environ)
    # Environment variables beat .env (load_dotenv never overrides), so the
    # portal always points at the API port chosen here.
    env["TALENT360_BACKEND_URL"] = f"http://{HOST}:{API_PORT}"

    def uvicorn(app, port, cwd, watch):
        cmd = [sys.executable, "-m", "uvicorn", app, "--host", HOST, "--port", str(port)]
        if reload:
            # Each server only watches its own code, so editing one never restarts the other.
            for directory in watch:
                cmd += ["--reload", "--reload-dir", str(directory)]
        return subprocess.Popen(cmd, cwd=cwd, env=env)

    say(f"\nStarting pipeline API on http://{HOST}:{API_PORT} ...")
    api = uvicorn("app.main:app", API_PORT, ROOT, [ROOT / "app", ROOT / "agents"])
    if not wait_until_up(f"http://{HOST}:{API_PORT}/", api):
        api.terminate()
        fail("The pipeline API did not start -- see the output above.")

    say(f"Starting portal on http://{HOST}:{PORTAL_PORT} ...")
    portal = uvicorn("app:app", PORTAL_PORT, PORTAL_DIR, [PORTAL_DIR])
    if not wait_until_up(f"http://{HOST}:{PORTAL_PORT}/login", portal):
        for proc in (api, portal):
            proc.terminate()
        fail("The portal did not start -- see the output above.")

    say("\n" + "=" * 72)
    say(f"  Talent 360i is running:  http://{HOST}:{PORTAL_PORT}/login")
    say(f"  Log in as: {', '.join(DEMO_ACCOUNTS)}")
    say("    password = TALENT360_DEMO_PASSWORD in .env")
    say("  Synthetic dataset users syn-u001 .. syn-u036")
    say("    password = TALENT360_SYNTHETIC_PASSWORD in .env")
    say("  Press Ctrl+C to stop everything.")
    say("=" * 72 + "\n")

    procs = {"pipeline API": api, "portal": portal}
    if worker:
        # Probation tracking from hire dates, reminders, and the status snapshot org-wide pages read.
        procs["lifecycle worker"] = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "lifecycle_worker.py"),
                                                      "--interval", "300"], cwd=ROOT, env=env)
    # `kill <pid>` (SIGTERM) shuts down like Ctrl+C, so the two servers are never orphaned.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        while True:
            for name, proc in procs.items():
                if proc.poll() is not None:
                    say(f"\nThe {name} exited (code {proc.returncode}); stopping.")
                    return proc.returncode or 1
            time.sleep(1)
    except KeyboardInterrupt:
        say("\nStopping ...")
        return 0
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main():
    parser = argparse.ArgumentParser(description="Set up and run Talent 360i locally.")
    parser.add_argument("--setup", action="store_true", help="configure and build data, then exit")
    parser.add_argument("--reseed", action="store_true", help="re-apply .env passwords to all login accounts")
    parser.add_argument("--rebuild", action="store_true", help="rebuild reference data from data/*.xlsx")
    parser.add_argument("--reload", action="store_true", help="auto-restart servers on code changes (dev)")
    parser.add_argument("--no-worker", action="store_true", help="don't start the lifecycle worker")
    args = parser.parse_args()

    say("Talent 360i local setup")
    check_python()
    check_dependencies()
    root_env = ensure_env_files()
    check_llm_key(root_env)
    ensure_data(rebuild=args.rebuild, reseed=args.reseed)
    if args.setup:
        say("\nSetup complete. Start the app with:  python start.py")
        return 0
    return start_servers(reload=args.reload, worker=not args.no_worker)


if __name__ == "__main__":
    sys.exit(main())
