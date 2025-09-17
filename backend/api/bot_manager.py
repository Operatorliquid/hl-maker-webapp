# backend/api/bot_manager.py
from __future__ import annotations
import os, sys, signal, threading, multiprocessing as mp
from dataclasses import dataclass
from typing import Optional, Dict, Deque, Any
from collections import deque
import logging
import time

from api import pidguard

# ---- tipos hints (opcionales) ----
try:
    from src.maker_bot import BotArgs  # type: ignore
    from src.adapter import HLConfig   # type: ignore
except Exception:
    from dataclasses import dataclass
    @dataclass
    class BotArgs:  # type: ignore
        ticker: str
        amount_per_level: float
        min_spread: float
        maker_only: bool
        ttl: float
        use_testnet: bool
        use_agent: bool
        agent_private_key: Optional[str]
    @dataclass
    class HLConfig:  # type: ignore
        private_key: str
        use_testnet: bool = False
        use_agent: bool = False
        agent_private_key: Optional[str] = None

log = logging.getLogger("bot_manager")

# ======================================================================
# Proceso hijo: corre el bot y reenvía logs a la queue
# ======================================================================
def _worker(log_q: mp.Queue, cfg_dict: Dict[str, Any], args_dict: Dict[str, Any]):
    # logging simple → queue
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    class _QH(logging.Handler):
        def emit(self, record):
            try:
                msg = self.format(record)
            except Exception:
                msg = str(record.getMessage())
            try:
                log_q.put_nowait(msg)
            except Exception:
                pass
    qh = _QH()
    qh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.handlers[:] = [qh]

    # Señales → salida limpia
    def _sig_handler(*_a):
        try: log_q.put_nowait("[STOP] Señal recibida, cerrando bot…")
        except Exception: pass
        os._exit(0)
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)

    # WATCH del padre: si muere el backend, el hijo sale
    parent_pid0 = os.getppid()
    def _parent_watch():
        while True:
            time.sleep(1.5)
            ppid = os.getppid()
            if ppid == 1 or ppid != parent_pid0:
                try: log_q.put_nowait("[STOP] Parent murió / cambió, saliendo…")
                except Exception: pass
                os._exit(0)
    threading.Thread(target=_parent_watch, daemon=True).start()

    # Imports pesados SOLO en el hijo
    from src.adapter import ExchangeAdapter, HLConfig as _HLConfig  # type: ignore
    from src.maker_bot import MakerBot, BotArgs as _BotArgs        # type: ignore

    cfg = _HLConfig(**cfg_dict)
    args = _BotArgs(**args_dict)

    root.info(f"[MANAGER] spawn bot for {args.ticker} testnet={args.use_testnet} agent={bool(args.agent_private_key)}")

    adapter = ExchangeAdapter(cfg)
    bot = MakerBot(adapter, args)

    try:
        bot.resolve_coin()
        bot.start_ws()
        bot.loop()   # el loop debería salir por SIGTERM o por su propia condición
    except KeyboardInterrupt:
        root.info("[MANAGER] KeyboardInterrupt en worker")
    except Exception as e:
        root.exception(f"[MANAGER] Excepción en worker: {e}")
        time.sleep(0.1)
    finally:
        try: log_q.put_nowait("[MANAGER] worker terminado")
        except Exception: pass

# ======================================================================
# Estado
# ======================================================================
@dataclass
class RunnerState:
    proc: Optional[mp.Process] = None
    started_at: float = 0.0

class BotRunner:
    def __init__(self, key: str):
        self.key = key
        self.state = RunnerState()
        self._log_q: mp.Queue = mp.Queue(maxsize=5000)
        self._logs: Deque[str] = deque(maxlen=8000)
        self._drainer: Optional[threading.Thread] = None

    def _start_drainer(self):
        if self._drainer and self._drainer.is_alive():
            return
        def _drain():
            while True:
                try:
                    line = self._log_q.get()
                except Exception:
                    break
                if line is None:
                    break
                self._logs.append(str(line))
                p = self.state.proc
                if (p is None) or (not p.is_alive()):
                    if self._log_q.empty():
                        break
        self._drainer = threading.Thread(target=_drain, daemon=True)
        self._drainer.start()

    def read_logs(self, n: int = 200):
        if n <= 0:
            return []
        return list(self._logs)[-n:]

    def start(self, cfg: HLConfig, args: BotArgs):
        # no dupliques
        if self.state.proc and self.state.proc.is_alive():
            return

        # limpia restos (por si quedó algo)
        pidguard.kill_key(self.key)

        # serializables
        cfg_d = {
            "private_key": cfg.private_key,
            "use_testnet": bool(cfg.use_testnet),
            "use_agent": bool(getattr(cfg, 'use_agent', False)),
            "agent_private_key": getattr(cfg, 'agent_private_key', None),
        }
        args_d = {
            "ticker": args.ticker,
            "amount_per_level": float(args.amount_per_level),
            "min_spread": float(args.min_spread),
            "maker_only": bool(args.maker_only),
            "ttl": float(args.ttl),
            "use_testnet": bool(args.use_testnet),
            "use_agent": bool(args.use_agent),
            "agent_private_key": getattr(args, 'agent_private_key', None),
        }

        proc = mp.get_context("spawn").Process(
            target=_worker, args=(self._log_q, cfg_d, args_d), name=f"hlbot-{self.key}"
        )
        proc.daemon = True
        proc.start()
        self.state = RunnerState(proc=proc, started_at=time.time())

        pidguard.write_pidfile(self.key, proc.pid)
        self._start_drainer()

        threading.Thread(target=self._watch_and_cleanup, args=(proc,), daemon=True).start()

    def _watch_and_cleanup(self, proc: mp.Process):
        proc.join()
        try:
            pidguard.remove_pidfile(self.key)
        finally:
            try: self._log_q.put_nowait(None)
            except Exception: pass

    def stop(self):
        p = self.state.proc
        if p and p.is_alive():
            try:
                os.kill(p.pid, signal.SIGTERM)
                p.join(timeout=3.0)
            except Exception:
                pass
            if p.is_alive():
                try:
                    os.kill(p.pid, signal.SIGKILL)
                    p.join(timeout=2.0)
                except Exception:
                    pass
        pidguard.kill_key(self.key)
        self.state = RunnerState(proc=None, started_at=0.0)
        try: self._log_q.put_nowait(None)
        except Exception: pass

    def get_adapter(self):
        return None

# ======================================================================
# Registro
# ======================================================================
class BotRegistry:
    def __init__(self):
        self._by_key: Dict[str, BotRunner] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[BotRunner]:
        return self._by_key.get(key)

    def start(self, key: str, cfg: HLConfig, args: BotArgs):
        with self._lock:
            br = self._by_key.get(key)
            if not br:
                br = BotRunner(key)
                self._by_key[key] = br
            br.start(cfg, args)

    def stop(self, key: str):
        with self._lock:
            br = self._by_key.get(key)
            if br:
                br.stop()

registry = BotRegistry()
