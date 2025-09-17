# backend/api/bot_manager.py
from __future__ import annotations
import os
import time
import signal
import logging
import threading
import multiprocessing as mp
from dataclasses import dataclass, asdict
from typing import Optional, Dict, Deque, Any, List
from collections import deque

from api import pidguard

# ===== Multiprocessing: usar SIEMPRE SPAWN (macOS/Railway) =====
CTX = mp.get_context("spawn")

# ===== Logger =====
log = logging.getLogger("bot_manager")
log.setLevel(logging.INFO)

# ===== Config watchdog (opcional) =====
STOP_ON_SILENCE_SEC = int(os.getenv("STOP_ON_SILENCE_SEC", "0") or "0")


# ------------------------------
# Worker: corre el MakerBot real
# ------------------------------
def _worker(key: str,
            cfg_dict: Dict[str, Any],
            args_dict: Dict[str, Any],
            log_q: mp.Queue,
            stop_evt: mp.Event) -> None:
    """
    Proceso hijo: reconstruye HLConfig/BotArgs, crea ExchangeAdapter + MakerBot y corre el loop.
    Reenvía todos los logs al padre vía QueueHandler.
    """
    try:
        # --- logging del hijo: mandar TODO al queue del padre
        import logging
        from logging.handlers import QueueHandler

        root = logging.getLogger()
        # limpiar handlers previos (si hubiera)
        while root.handlers:
            try:
                root.removeHandler(root.handlers[0])
            except Exception:
                break
        qh = QueueHandler(log_q)
        qh.setLevel(logging.INFO)
        root.addHandler(qh)
        root.setLevel(logging.INFO)

        # Log de arranque
        logging.info(f"[CHILD] starting worker key={key}")

        # Importes dentro del hijo (import lazy para SPAWN)
        from src.adapter import HLConfig, ExchangeAdapter
        from src.maker_bot import BotArgs, MakerBot

        # Re-armar dataclasses
        cfg = HLConfig(**cfg_dict)
        args = BotArgs(**args_dict)

        # Construir adapter + bot
        adapter = ExchangeAdapter(cfg)
        bot = MakerBot(adapter, args)

        # Secuencia de ejecución (como en tu maker_bot)
        bot.resolve_coin()
        bot.start_ws()
        bot.loop()  # bloqueante hasta que termine

        logging.info("[CHILD] worker finished normally")

    except Exception as e:
        try:
            logging.exception(f"[CHILD] worker crashed: {e}")
        finally:
            # En caso de error, salimos con código distinto de cero
            os._exit(1)


# --------------------------------
# Listener de logs en el proceso padre
# --------------------------------
def _log_listener(q: mp.Queue, buffer: Deque[str], stop_evt: threading.Event):
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    while not stop_evt.is_set():
        try:
            rec = q.get(timeout=0.5)
        except Exception:
            continue
        try:
            # Si el hijo mandó un LogRecord, lo formateamos
            line = fmt.format(rec)
        except Exception:
            # Si es texto crudo, lo casteamos
            line = str(rec)
        buffer.append(line)


# ------------------------------
# Runner: estado por usuario/bot
# ------------------------------
class BotRunner:
    def __init__(self, key: str):
        self.key = key
        self.proc: Optional[mp.Process] = None
        self.log_q: mp.Queue = CTX.Queue()
        self._log_buf: Deque[str] = deque(maxlen=2000)
        self._log_thread: Optional[threading.Thread] = None
        self._log_thread_stop = threading.Event()
        self._lock = threading.Lock()
        self.stop_evt: mp.Event = CTX.Event()
        self.started_at: Optional[float] = None
        self.last_beat: float = time.time()

    # --- control de logs ---
    def _start_log_listener(self):
        self._log_thread_stop.clear()
        t = threading.Thread(target=_log_listener, args=(self.log_q, self._log_buf, self._log_thread_stop), daemon=True)
        t.start()
        self._log_thread = t

    def _stop_log_listener(self):
        try:
            self._log_thread_stop.set()
        except Exception:
            pass

    def read_logs(self, max_lines: int = 200) -> List[str]:
        """Devuelve y DRENA hasta max_lines del buffer."""
        lines: List[str] = []
        with self._lock:
            for _ in range(min(max_lines, len(self._log_buf))):
                try:
                    lines.append(self._log_buf.popleft())
                except IndexError:
                    break
        return lines

    # --- lifecycle ---
    def start(self, cfg, args):
        from dataclasses import asdict
        if self.proc and self.proc.is_alive():
            log.info(f"[RUNNER] {self.key} ya estaba vivo, lo paro antes de reiniciar")
            self.stop()

        cfg_d = asdict(cfg)
        args_d = asdict(args)

        self.stop_evt = CTX.Event()
        self.proc = CTX.Process(
            target=_worker,
            args=(self.key, cfg_d, args_d, self.log_q, self.stop_evt),
            name=f"hlbot-{self.key}",
            daemon=True,  # si muere uvicorn, muere el hijo
        )
        self.proc.start()
        self.started_at = time.time()
        self.last_beat = self.started_at

        # Log listener
        self._start_log_listener()

        # PID file para matar por fuera si hiciera falta
        try:
            pidguard.write_pidfile(self.key, self.proc.pid)
        except Exception as e:
            log.warning(f"[RUNNER] write_pidfile error: {e}")

        log.info(f"[RUNNER] started key={self.key} pid={self.proc.pid}")

    def stop(self, timeout: float = 3.0):
        p = self.proc
        if not p:
            return
        try:
            log.info(f"[RUNNER] stopping key={self.key} pid={getattr(p,'pid',None)}")
            # Señal suave al hijo (si la usás dentro de maker_bot)
            try:
                self.stop_evt.set()
            except Exception:
                pass

            # Espera suave
            p.join(timeout)
            # SIGTERM si sigue vivo
            if p.is_alive():
                try:
                    os.kill(p.pid, signal.SIGTERM)
                except Exception:
                    pass
                p.join(1.5)
            # SIGKILL si aún sigue
            if p.is_alive():
                try:
                    os.kill(p.pid, signal.SIGKILL)
                except Exception:
                    pass
                p.join(0.5)
        finally:
            # Listener y pidfile
            self._stop_log_listener()
            try:
                pidguard.kill_key(self.key)
            except Exception:
                pass
            self.proc = None

    def is_alive(self) -> bool:
        return bool(self.proc and self.proc.is_alive())

    def touch(self):
        self.last_beat = time.time()


# ------------------------------
# Registry (multiusuario)
# ------------------------------
class Registry:
    def __init__(self):
        self._runners: Dict[str, BotRunner] = {}
        self._lock = threading.Lock()
        self._stop_on_silence = STOP_ON_SILENCE_SEC
        # Watchdog de inactividad (si configurado)
        if self._stop_on_silence > 0:
            threading.Thread(target=self._watchdog, daemon=True).start()

    def _watchdog(self):
        while True:
            try:
                if self._stop_on_silence > 0:
                    now = time.time()
                    with self._lock:
                        for key, r in list(self._runners.items()):
                            if r.is_alive():
                                idle = now - r.last_beat
                                if idle > self._stop_on_silence:
                                    log.info(f"[WD] stopping {key} por inactividad ({int(idle)}s)")
                                    try:
                                        r.stop()
                                    finally:
                                        try:
                                            pidguard.kill_key(key)
                                        except Exception:
                                            pass
            except Exception:
                pass
            time.sleep(5)

    # API pública usada por main.py
    def start(self, key: str, cfg, args):
        with self._lock:
            r = self._runners.get(key)
            if not r:
                r = BotRunner(key)
                self._runners[key] = r
        r.start(cfg, args)
        return True

    def stop(self, key: str):
        with self._lock:
            r = self._runners.get(key)
        if r:
            r.stop()
            return True
        return False

    def get(self, key: str) -> Optional[BotRunner]:
        with self._lock:
            return self._runners.get(key)

    def touch(self, key: str):
        with self._lock:
            r = self._runners.get(key)
        if r:
            r.touch()

    # util global
    def stop_all(self) -> Dict[str, bool]:
        out = {}
        with self._lock:
            keys = list(self._runners.keys())
        for k in keys:
            out[k] = self.stop(k)
        return out


# Export singleton
registry = Registry()
