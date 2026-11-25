"""Start the FamilyCart development stack in one terminal."""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from contextlib import suppress
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
DATABASE_URL_KEYS = ("FAMILYCART_USER_DATABASE_URL", "FAMILYCART_DATABASE_URL")
FRONTEND = ROOT / "frontend"
HOST = "127.0.0.1"
APP_URL = f"http://{HOST}:5173"
MAPS_PORT = 8004
WINDOWS = os.name == "nt"
HTTP = build_opener(ProxyHandler({}))


def read_settings(path):
    settings = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key.isidentifier():
            raise RuntimeError(f"Expected a KEY=value setting in {path.name} on line {number}.")
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        settings[key] = value
    return settings


def load_settings(supabase=False, *, use_saved_pooler=True):
    path = ROOT / ".env"
    if not path.is_file():
        raise RuntimeError("Copy .env.example to .env and fill in the settings before launching.")
    settings = read_settings(path)
    # Older installations kept the hosted credentials in a separate file.
    overlay = ROOT / ".env.supabase"
    if supabase and overlay.is_file():
        settings.update(read_settings(overlay))
    settings["FAMILYCART_DB_MODE"] = "supabase"

    required = (
        "FAMILYCART_JWT_SECRET", "FAMILYCART_USER_DATABASE_URL", "FAMILYCART_DATABASE_URL",
        "FAMILYCART_REDIS_URL", "FAMILYCART_GOOGLE_CLIENT_ID", "VITE_GOOGLE_CLIENT_ID",
    )
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise RuntimeError(f"Missing settings in .env: {', '.join(missing)}.")
    for key in DATABASE_URL_KEYS:
        if not settings[key].startswith(("postgresql://", "postgres://")):
            raise RuntimeError(f"Set {key} to a Supabase PostgreSQL URL.")
        pooler = settings.get("FAMILYCART_SESSION_POOLER_HOST")
        direct = re.fullmatch(r"db\.([a-z0-9]+)\.supabase\.co", urlsplit(settings[key]).hostname or "")
        if not pooler and direct and use_saved_pooler:
            saved = ROOT / ".familycart" / f"{direct[1]}.pooler"
            if saved.is_file():
                pooler = saved.read_text(encoding="ascii").strip()
        if pooler:
            settings[key] = session_pooler_url(settings[key], pooler)
    if len(settings["FAMILYCART_JWT_SECRET"].encode("utf-8")) < 32:
        raise RuntimeError("FAMILYCART_JWT_SECRET in .env must be at least 32 bytes.")
    if settings["FAMILYCART_GOOGLE_CLIENT_ID"] != settings["VITE_GOOGLE_CLIENT_ID"]:
        raise RuntimeError("The two Google client IDs in .env must match.")
    return {**os.environ, **settings}


def session_pooler_url(url, host):
    if not re.fullmatch(r"[a-z0-9-]+\.pooler\.supabase\.com", host):
        raise RuntimeError("FAMILYCART_SESSION_POOLER_HOST must contain only the Supabase pooler hostname.")
    parts = urlsplit(url)
    direct = re.fullmatch(r"db\.([a-z0-9]+)\.supabase\.co", parts.hostname or "")
    if direct:
        login = f"{parts.username}.{direct[1]}"
    elif parts.hostname == host and parts.username and "." in parts.username:
        login = parts.username
    else:
        raise RuntimeError("The configured database URL must belong to the direct project host or its session pooler.")
    return urlunsplit(parts._replace(netloc=f"{login}:{parts.password}@{host}:5432"))


def connect_db(supabase):
    env = load_settings(supabase, use_saved_pooler=False)
    host = urlsplit(env[DATABASE_URL_KEYS[0]]).hostname or ""
    project = re.fullmatch(r"db\.([a-z0-9]+)\.supabase\.co", host)
    python = backend_python()
    check = [python, "-m", "backend.supabase_cutover", "check"]
    if not project:
        # An explicit pooler is already configured.
        subprocess.run(check, cwd=ROOT, env=env, check=True)
        return
    ref = project[1]
    if any(urlsplit(env[k]).hostname != host for k in DATABASE_URL_KEYS):
        raise RuntimeError("Both database URLs must point to the same Supabase project.")
    workdir = ROOT / ".familycart" / "cli"
    workdir.mkdir(parents=True, exist_ok=True)
    binary = shutil.which("supabase")
    if binary:
        cli = [binary]
    else:
        npx = shutil.which("npx.cmd" if WINDOWS else "npx")
        if not npx:
            raise RuntimeError("Install Node.js with npx or the Supabase CLI to link the project.")
        cli = [npx, "--yes", "supabase@2.79.0"]
    if not os.environ.get("SUPABASE_ACCESS_TOKEN"):
        print("Authorise the Supabase CLI in your browser to look up this project's IPv4 endpoint.", flush=True)
        subprocess.run([*cli, "login"], cwd=workdir, check=True)
    subprocess.run([*cli, "link", "--project-ref", ref], cwd=workdir, check=True)
    metadata = workdir / "supabase" / ".temp"
    if not (metadata / "pooler-url").is_file() or not (metadata / "project-ref").is_file():
        raise RuntimeError("Supabase CLI linked the project but did not return its pooler metadata.")
    pooled = urlsplit((metadata / "pooler-url").read_text(encoding="utf-8").strip())
    if ((metadata / "project-ref").read_text().strip() != ref
            or not (pooled.username or "").endswith("." + ref) or not pooled.hostname):
        raise RuntimeError("Supabase CLI returned pooler metadata for a different project.")
    for key in DATABASE_URL_KEYS:
        env[key] = session_pooler_url(env[key], pooled.hostname)
    subprocess.run(check, cwd=ROOT, env=env, check=True)
    saved = ROOT / ".familycart" / f"{ref}.pooler"
    temporary = saved.with_suffix(".pooler.tmp")
    temporary.write_text(pooled.hostname + "\n", encoding="ascii")
    temporary.replace(saved)
    print(f"Verified both service logins and saved {pooled.hostname} for future launches.")


def lock_launcher(handle):
    try:
        if WINDOWS:
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError("FamilyCart is already starting or running in another launcher window.") from None


def check_app_ports():
    for port in (8000, 8001, 8002, 8003, MAPS_PORT, 5173):
        try:
            with socket.create_connection((HOST, port), timeout=0.25):
                pass
        except OSError:
            continue
        raise RuntimeError(f"Port {port} is already in use. Stop the existing app process and try again.")


def backend_python():
    requirements = ROOT / "backend" / "requirements.txt"
    venv = ROOT / ".venv"
    python = venv / ("Scripts/python.exe" if WINDOWS else "bin/python")
    stamp = venv / ".familycart-requirements.txt"
    if not python.is_file():
        print("Creating the Python environment...")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True, cwd=ROOT)
        stamp.unlink(missing_ok=True)
    pinned = requirements.read_bytes()
    if not stamp.is_file() or stamp.read_bytes() != pinned:
        print("Installing backend dependencies...")
        stamp.unlink(missing_ok=True)
        subprocess.run([python, "-m", "pip", "install", "-r", requirements], check=True, cwd=ROOT)
        stamp.write_bytes(pinned)
    return python


def frontend_command():
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Install Node.js 22.12+ or 20.19+ before starting FamilyCart.")
    modules = FRONTEND / "node_modules"
    vite = modules / "vite" / "bin" / "vite.js"
    expected = json.loads((FRONTEND / "package-lock.json").read_text(encoding="utf-8"))["packages"]
    try:
        installed = json.loads((modules / ".package-lock.json").read_text(encoding="utf-8"))["packages"]
    except (OSError, ValueError, KeyError):
        installed = {}
    required = {path for path, package in expected.items() if path and not package.get("optional")}
    if vite.is_file() and required <= installed.keys() and all(
        package.get("integrity") == expected.get(path, {}).get("integrity")
        and package.get("version") == expected.get(path, {}).get("version")
        and (FRONTEND / path / "package.json").is_file()
        for path, package in installed.items()
    ):
        return [node, vite]

    npm = shutil.which("npm.cmd" if WINDOWS else "npm")
    if not npm:
        raise RuntimeError("npm is needed to install the frontend dependencies.")
    print("Installing frontend dependencies...")
    subprocess.run([npm, "ci"], check=True, cwd=FRONTEND)
    return [node, vite]


def start_data_services():
    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Install Docker Desktop to run the FamilyCart data services.")
    version_command = [docker, "version", "--format", "{{.Server.Os}}"]
    engine = subprocess.run(version_command, cwd=ROOT, capture_output=True, text=True, timeout=10)
    if engine.returncode and WINDOWS:
        print("Starting Docker Desktop...")
        subprocess.run([docker, "desktop", "start", "--timeout", "120"], cwd=ROOT, check=True, timeout=125)
        engine = subprocess.run(version_command, cwd=ROOT, capture_output=True, text=True, timeout=10)
    if engine.returncode:
        raise RuntimeError(f"Cannot connect to Docker. {engine.stderr.strip()}")
    if engine.stdout.strip() != "linux":
        raise RuntimeError("Switch Docker Desktop to Linux containers mode before launching FamilyCart.")

    print("Starting local Redis for FamilyCart...")
    subprocess.run([docker, "compose", "up", "-d", "--wait", "--wait-timeout", "120", "redis"], cwd=ROOT, check=True)


def check_processes(processes):
    for name, process in processes.items():
        if process.poll() is not None:
            raise RuntimeError(f"{name} exited with code {process.returncode}. See its output above.")


def wait_for_http(processes, endpoints):
    pending = dict(endpoints)
    deadline = time.monotonic() + 45
    while pending and time.monotonic() < deadline:
        check_processes(processes)
        for name, url in list(pending.items()):
            try:
                with HTTP.open(url, timeout=0.5) as response:
                    if response.status == 200:
                        del pending[name]
            except (URLError, TimeoutError):
                pass
        if pending:
            time.sleep(0.2)
    if pending:
        raise RuntimeError(f"Timed out waiting for {', '.join(pending)}.")


def stop_processes(processes):
    running = [process for process in processes.values() if process.poll() is None]
    for process in running:
        with suppress(OSError):
            if WINDOWS:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()
    deadline = time.monotonic() + 3
    for process in running:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with suppress(OSError):
                process.kill()
            process.wait()


def run(no_browser, supabase=False):
    env = load_settings(supabase)
    check_app_ports()
    python = backend_python()
    print("Checking both Supabase database logins...", flush=True)
    subprocess.run([python, "-m", "backend.supabase_cutover", "check"], cwd=ROOT, env=env, check=True)
    frontend = frontend_command()
    start_data_services()
    processes = {}

    def start(name, command, cwd):
        processes[name] = subprocess.Popen(
            command, cwd=cwd, env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if WINDOWS else 0,
            start_new_session=not WINDOWS,
        )

    def start_api(name, module, port):
        start(name, [python, "-m", "uvicorn", f"{module}:app", "--host", HOST, "--port", str(port)], ROOT)

    try:
        print("Starting the FamilyCart services and frontend...")
        start_api("Auth", "backend.services.auth_service.app", 8001)
        start_api("FamilyCart", "backend.services.familycart_service.app", 8002)
        start_api("Maps", "backend.services.maps_service.app", MAPS_PORT)
        start("Vite", frontend, FRONTEND)

        # Auth initializes the user database shared by Refresh and the Gateway.
        wait_for_http(processes, {"Auth": f"http://{HOST}:8001/docs"})
        start_api("Refresh", "backend.services.refresh_service.app", 8003)
        start_api("API Gateway", "backend.api_gateway.app", 8000)
        wait_for_http(processes, {
            "FamilyCart": f"http://{HOST}:8002/docs",
            "Maps": f"http://{HOST}:{MAPS_PORT}/health",
            "Refresh": f"http://{HOST}:8003/docs",
            "API Gateway": f"http://{HOST}:8000/health",
        })
        wait_for_http(processes, {"frontend proxy": f"{APP_URL}/api/v1/auth/google/config"})

        print(f"FamilyCart is ready at {APP_URL}. Press Ctrl+C to stop.")
        if not no_browser:
            webbrowser.open(APP_URL)
        while True:
            check_processes(processes)
            time.sleep(0.5)
    finally:
        stop_processes(processes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically.")
    parser.add_argument("--supabase", action="store_true", help="Use the database settings in .env.supabase.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--connect-db", action="store_true", help="Discover and verify this project's IPv4 session pooler.")
    actions.add_argument("--check-db", action="store_true", help="Verify both hosted service logins and schema access.")
    actions.add_argument("--migrate-local", action="store_true", help="Copy and verify legacy PostgreSQL data in Supabase.")
    args = parser.parse_args()
    try:
        state = ROOT / ".familycart"
        state.mkdir(exist_ok=True)
        with (state / "launcher.lock").open("a+b") as lock:
            lock_launcher(lock)
            if args.connect_db:
                connect_db(args.supabase)
            elif args.check_db or args.migrate_local:
                env = load_settings(args.supabase)
                action = "migrate" if args.migrate_local else "check"
                subprocess.run([backend_python(), "-m", "backend.supabase_cutover", action], cwd=ROOT, env=env, check=True)
            else:
                run(args.no_browser, args.supabase)
    except KeyboardInterrupt:
        print("FamilyCart stopped.")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"FamilyCart could not run: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
