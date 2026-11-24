"""Start the FamilyCart development stack in one terminal."""

import argparse
import json
import os
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
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
HOST = "127.0.0.1"
APP_URL = f"http://{HOST}:5173"
MAPS_PORT = 8004
WINDOWS = os.name == "nt"
HTTP = build_opener(ProxyHandler({}))


def load_settings():
    path = ROOT / ".env"
    if not path.is_file():
        raise RuntimeError("Copy .env.example to .env and fill in the settings before launching.")

    settings = {}
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key.isidentifier():
            raise RuntimeError(f"Expected a KEY=value setting in .env on line {number}.")
        if len(value) >= 2 and value[0] in {"'", '"'} and value[-1] == value[0]:
            value = value[1:-1]
        settings[key] = value

    required = (
        "FAMILYCART_JWT_SECRET", "FAMILYCART_USER_DATABASE_URL", "FAMILYCART_DATABASE_URL",
        "FAMILYCART_REDIS_URL", "FAMILYCART_GOOGLE_CLIENT_ID", "VITE_GOOGLE_CLIENT_ID",
    )
    missing = [key for key in required if not settings.get(key)]
    if missing:
        raise RuntimeError(f"Missing settings in .env: {', '.join(missing)}.")
    if len(settings["FAMILYCART_JWT_SECRET"].encode("utf-8")) < 32:
        raise RuntimeError("FAMILYCART_JWT_SECRET in .env must be at least 32 bytes.")
    if settings["FAMILYCART_GOOGLE_CLIENT_ID"] != settings["VITE_GOOGLE_CLIENT_ID"]:
        raise RuntimeError("The two Google client IDs in .env must match.")
    return {**os.environ, **settings}


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

    print("Waiting for PostgreSQL and Redis...")
    subprocess.run([docker, "compose", "up", "-d", "--wait", "--wait-timeout", "120"], cwd=ROOT, check=True)


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


def run(no_browser):
    env = load_settings()
    check_app_ports()
    frontend = frontend_command()
    start_data_services()
    python = backend_python()
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
    args = parser.parse_args()
    try:
        state = ROOT / ".familycart"
        state.mkdir(exist_ok=True)
        with (state / "launcher.lock").open("a+b") as lock:
            lock_launcher(lock)
            run(args.no_browser)
    except KeyboardInterrupt:
        print("FamilyCart stopped.")
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"FamilyCart could not run: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
