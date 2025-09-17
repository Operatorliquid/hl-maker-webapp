# backend/api/pidguard.py
import os, json, time, tempfile
from typing import Optional, List
import psutil

def _tmpdir():
    return tempfile.gettempdir()

def pidfile_path(key: str) -> str:
    safe = "".join(ch for ch in key if ch.isalnum() or ch in ("-","_",":"))
    return os.path.join(_tmpdir(), f"hlmaker-{safe}.pid")

def write_pidfile(key: str, pid: int):
    path = pidfile_path(key)
    data = {"pid": int(pid), "key": key, "ts": int(time.time())}
    with open(path, "w") as f:
        json.dump(data, f)

def read_pid(key: str) -> Optional[int]:
    path = pidfile_path(key)
    try:
        with open(path, "r") as f:
            return int(json.load(f).get("pid"))
    except Exception:
        return None

def remove_pidfile(key: str):
    path = pidfile_path(key)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass

def _kill_pid(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        p.terminate()
    except Exception:
        pass
    try:
        p.wait(timeout=3)
        return True
    except Exception:
        try:
            p.kill()
            p.wait(timeout=2)
            return True
        except Exception:
            return False

def kill_key(key: str) -> bool:
    pid = read_pid(key)
    if not pid:
        return True
    ok = _kill_pid(pid)
    if ok:
        remove_pidfile(key)
    return ok

def cleanup_dead() -> List[str]:
    removed = []
    for name in os.listdir(_tmpdir()):
        if not name.startswith("hlmaker-") or not name.endswith(".pid"):
            continue
        path = os.path.join(_tmpdir(), name)
        try:
            with open(path, "r") as f:
                pid = int(json.load(f).get("pid"))
            if not psutil.pid_exists(pid):
                os.remove(path)
                removed.append(name)
        except Exception:
            try: os.remove(path)
            except Exception: pass
            removed.append(name)
    return removed

def kill_all() -> List[int]:
    killed = []
    for name in os.listdir(_tmpdir()):
        if not name.startswith("hlmaker-") or not name.endswith(".pid"):
            continue
        key = name[len("hlmaker-"):-4]
        pid = read_pid(key)
        if pid and _kill_pid(pid):
            remove_pidfile(key)
            killed.append(pid)
    return killed
