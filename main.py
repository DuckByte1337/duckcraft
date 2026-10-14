import asyncio
import hashlib
import json
import re
import shutil
import subprocess
import threading
import webbrowser
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

HOST, PORT = "127.0.0.1", 8000
BASE_DIR = Path(__file__).resolve().parent
SERVERS_DIR = BASE_DIR / "servers"
SERVERS_DIR.mkdir(exist_ok=True)

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

PAPER_API = "https://fill.papermc.io/v3"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
HEADERS = {"User-Agent": "DUCKCRAFT/0.2 (https://github.com/DuckByte1337/DUCKCRAFT)"}

NAME_RE = re.compile(r"[A-Za-z0-9_-](?:[A-Za-z0-9 _-]{0,30}[A-Za-z0-9_-])?")
VERSION_RE = re.compile(r"[A-Za-z0-9._+-]{1,40}")

active_server_name: str | None = None
process: asyncio.subprocess.Process | None = None
phase = "offline"
clients: set[WebSocket] = set()
logs_history: list[str] = []


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        self.message, self.status = message, status


class CreateServerRequest(BaseModel):
    name: str
    server_type: str
    version: str
    accept_eula: bool
    ram_gb: int = Field(default=2, ge=1, le=32)


class CommandRequest(BaseModel):
    command: str


class PropertiesRequest(BaseModel):
    values: dict[str, str]


def is_running() -> bool:
    return process is not None and process.returncode is None


async def graceful_stop(timeout: float = 30.0) -> None:
    if not is_running():
        return
    with suppress(Exception):
        process.stdin.write(b"stop\n")
        await process.stdin.drain()
    try:
        await asyncio.wait_for(process.wait(), timeout)
    except asyncio.TimeoutError:
        process.kill()


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await graceful_stop()


app = FastAPI(title="DUCKCRAFT", lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError):
    return JSONResponse({"error": exc.message}, status_code=exc.status)


def origin_allowed(host: str | None, origin: str | None) -> bool:
    if host not in ALLOWED_HOSTS:
        return False
    return origin is None or origin.split("://", 1)[-1] in ALLOWED_HOSTS


@app.middleware("http")
async def local_only_guard(request: Request, call_next):
    if not origin_allowed(request.headers.get("host"), request.headers.get("origin")):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    return await call_next(request)


@lru_cache(maxsize=1)
def get_java_info() -> dict:
    if not shutil.which("java"):
        return {"installed": False, "version": None}
    try:
        res = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=5)
        first_line = (res.stderr or res.stdout).splitlines()[0]
        return {"installed": True, "version": first_line.replace('"', "")}
    except Exception:
        return {"installed": True, "version": "Installed (version unknown)"}


def read_meta(server_dir: Path) -> dict:
    try:
        return json.loads((server_dir / "duckcraft.json").read_text())
    except Exception:
        return {}


def get_server_dir(name: str) -> Path:
    if not NAME_RE.fullmatch(name or ""):
        raise ApiError("Invalid server name.")
    server_dir = SERVERS_DIR / name
    if not (server_dir / "server.jar").is_file():
        raise ApiError("Server does not exist.", 404)
    return server_dir


async def broadcast(text: str) -> None:
    logs_history.append(text)
    if len(logs_history) > 1000:
        logs_history.pop(0)
    for ws in list(clients):
        try:
            await ws.send_text(text)
        except Exception:
            clients.discard(ws)


async def watch_process(proc: asyncio.subprocess.Process) -> None:
    global phase
    try:
        while line := await proc.stdout.readline():
            text = line.decode("utf-8", errors="replace")
            if phase == "starting" and "Done (" in text:
                phase = "online"
            await broadcast(text)
    finally:
        code = await proc.wait()
        if process is proc:
            phase = "offline"
        await broadcast(f"[DUCKCRAFT] Server process ended (exit code {code}).\n")


async def download(client: httpx.AsyncClient, url: str, dest: Path, algo: str, expected: str | None) -> None:
    digest = hashlib.new(algo)
    tmp = dest.with_suffix(".part")
    try:
        async with client.stream("GET", url, follow_redirects=True) as res:
            res.raise_for_status()
            with tmp.open("wb") as f:
                async for chunk in res.aiter_bytes(65536):
                    digest.update(chunk)
                    f.write(chunk)
        if expected and digest.hexdigest().lower() != expected.lower():
            raise ValueError("checksum mismatch, the download was corrupted")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)


async def resolve_paper(client: httpx.AsyncClient, version: str) -> tuple[str, str, str | None]:
    res = await client.get(f"{PAPER_API}/projects/paper/versions/{quote(version)}/builds/latest")
    if res.status_code == 404:
        raise ApiError(f"Paper has no build for {version}.", 404)
    res.raise_for_status()
    dl = res.json()["downloads"]["server:default"]
    return dl["url"], "sha256", (dl.get("checksums") or {}).get("sha256")


async def resolve_vanilla(client: httpx.AsyncClient, version: str) -> tuple[str, str, str | None]:
    manifest = await client.get(MOJANG_MANIFEST)
    manifest.raise_for_status()
    entry = next((v for v in manifest.json()["versions"] if v["id"] == version), None)
    if not entry:
        raise ApiError(f"Vanilla version {version} not found.", 404)
    meta = await client.get(entry["url"])
    meta.raise_for_status()
    dl = meta.json()["downloads"].get("server")
    if not dl:
        raise ApiError(f"Vanilla {version} has no server download.", 404)
    return dl["url"], "sha1", dl.get("sha1")


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/logo.svg")
def logo():
    return FileResponse(BASE_DIR / "logo.svg", media_type="image/svg+xml")


@app.get("/api/system")
def get_system():
    global active_server_name
    running = is_running()

    servers = []
    for p in sorted(SERVERS_DIR.iterdir(), key=lambda x: x.name.lower()):
        if p.is_dir() and (p / "server.jar").is_file():
            meta = read_meta(p)
            servers.append({
                "name": p.name,
                "type": meta.get("type", "unknown"),
                "version": meta.get("version", "unknown"),
                "ram_gb": meta.get("ram_gb", 2),
                "is_running": running and active_server_name == p.name,
            })

    names = {s["name"] for s in servers}
    if active_server_name not in names:
        active_server_name = servers[0]["name"] if servers else None

    return {
        "java": get_java_info(),
        "active_server": active_server_name,
        "is_running": running,
        "state": phase if running else "offline",
        "servers": servers,
    }


@app.get("/api/versions")
async def get_versions(type: str = "paper"):
    if type not in ("paper", "vanilla"):
        raise ApiError("Unknown server type.")
    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=10.0) as client:
            if type == "paper":
                res = await client.get(f"{PAPER_API}/projects/paper")
                res.raise_for_status()
                data = res.json().get("versions", {})
                if isinstance(data, dict):
                    versions = [v for group in data.values() if isinstance(group, list) for v in group]
                else:
                    versions = list(data)
            else:
                res = await client.get(MOJANG_MANIFEST)
                res.raise_for_status()
                versions = [v["id"] for v in res.json().get("versions", []) if v["type"] == "release"]
    except Exception as e:
        raise ApiError(f"Could not load versions: {e}", 502)
    return {"versions": versions}


@app.post("/api/servers/select")
def select_server(name: str):
    global active_server_name
    if is_running() and active_server_name != name:
        raise ApiError("Stop the running server before switching.")
    get_server_dir(name)
    if active_server_name != name:
        active_server_name = name
        logs_history.clear()
    return {"status": "ok", "active_server": active_server_name}


@app.post("/api/servers/create")
async def create_server(data: CreateServerRequest):
    global active_server_name
    if not data.accept_eula:
        raise ApiError("You must accept the Minecraft EULA to create a server.")
    if data.server_type not in ("paper", "vanilla"):
        raise ApiError("Unknown server type.")
    if not VERSION_RE.fullmatch(data.version):
        raise ApiError("Invalid version.")

    name = data.name.strip()
    if not NAME_RE.fullmatch(name):
        raise ApiError("Use 1-32 letters, numbers, spaces, - or _ for the name.")

    server_path = SERVERS_DIR / name
    if server_path.exists():
        raise ApiError("A server with that name already exists.", 409)
    server_path.mkdir()

    try:
        async with httpx.AsyncClient(headers=HEADERS, timeout=120.0) as client:
            resolver = resolve_paper if data.server_type == "paper" else resolve_vanilla
            url, algo, checksum = await resolver(client, data.version)
            await download(client, url, server_path / "server.jar", algo, checksum)
    except ApiError:
        shutil.rmtree(server_path, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(server_path, ignore_errors=True)
        raise ApiError(f"Download failed: {e}", 502)

    (server_path / "eula.txt").write_text("eula=true\n")
    (server_path / "duckcraft.json").write_text(json.dumps({
        "type": data.server_type, "version": data.version, "ram_gb": data.ram_gb,
    }))

    active_server_name = name
    logs_history.clear()
    return {"status": "created", "name": name}


PROP_KEY_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
PROP_INT_KEYS = {"server-port", "max-players", "view-distance", "simulation-distance", "spawn-protection"}


def active_properties_path() -> Path:
    if not active_server_name:
        raise ApiError("No server selected.")
    return get_server_dir(active_server_name) / "server.properties"


def split_property(line: str) -> tuple[str, str] | None:
    stripped = line.lstrip()
    if not stripped or stripped[0] in "#!" or "=" not in stripped:
        return None
    key, value = stripped.split("=", 1)
    return key.strip(), value


@app.get("/api/properties")
def get_properties():
    path = active_properties_path()
    if not path.is_file():
        return {"exists": False, "running": is_running(), "properties": {}}
    props = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if pair := split_property(line):
            props[pair[0]] = pair[1]
    return {"exists": True, "running": is_running(), "properties": props}


@app.put("/api/properties")
def save_properties(data: PropertiesRequest):
    if is_running():
        raise ApiError("Stop the server before changing its settings.")
    path = active_properties_path()
    if not path.is_file():
        raise ApiError("server.properties does not exist yet. Start the server once to create it.", 404)

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    existing = {pair[0] for line in lines if (pair := split_property(line))}

    for key, value in data.values.items():
        if not PROP_KEY_RE.fullmatch(key) or key not in existing:
            raise ApiError(f"Unknown setting: {key}")
        if "\n" in value or "\r" in value:
            raise ApiError(f"{key} must be a single line.")
        if key in PROP_INT_KEYS and not value.isdigit():
            raise ApiError(f"{key} must be a whole number.")

    out = []
    for line in lines:
        pair = split_property(line)
        if pair and pair[0] in data.values:
            out.append(f"{pair[0]}={data.values[pair[0]]}")
        else:
            out.append(line)

    shutil.copy2(path, path.with_name("server.properties.bak"))
    tmp = path.with_name("server.properties.tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"status": "saved", "changed": len(data.values)}


@app.post("/api/start")
async def start_server():
    global process, phase
    if is_running():
        return {"status": "already running"}
    if not active_server_name:
        raise ApiError("No server selected.")
    if not get_java_info()["installed"]:
        raise ApiError("Java is not installed or not on your PATH.")

    server_dir = get_server_dir(active_server_name)
    ram_gb = read_meta(server_dir).get("ram_gb", 2)

    logs_history.clear()
    phase = "starting"
    process = await asyncio.create_subprocess_exec(
        "java", f"-Xmx{int(ram_gb)}G", "-jar", "server.jar", "nogui",
        cwd=server_dir,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    asyncio.create_task(watch_process(process))
    return {"status": "started"}


@app.post("/api/stop")
async def stop_server():
    global phase
    if not is_running():
        return {"status": "not running"}
    phase = "stopping"
    with suppress(Exception):
        process.stdin.write(b"stop\n")
        await process.stdin.drain()
    return {"status": "stopping"}


@app.post("/api/kill")
def kill_server():
    if is_running():
        process.kill()
    return {"status": "killed"}


@app.post("/api/command")
async def send_command(data: CommandRequest):
    command = " ".join(data.command.split("\n")).strip()
    if not command:
        raise ApiError("Empty command.")
    if not is_running():
        raise ApiError("Server is offline.")
    process.stdin.write(f"{command}\n".encode())
    await process.stdin.drain()
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_logs(ws: WebSocket):
    if not origin_allowed(ws.headers.get("host"), ws.headers.get("origin")):
        await ws.close(code=1008)
        return
    await ws.accept()
    clients.add(ws)
    try:
        for line in list(logs_history):
            await ws.send_text(line)
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    import uvicorn

    threading.Timer(1.0, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    uvicorn.run(app, host=HOST, port=PORT)
