# src/adapter.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, Any
import logging
import math

from eth_account import Account
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

log = logging.getLogger(__name__)

# -------- Builder de Based (igual que el bot original) --------
BASED_BUILDER = {
    "b": "0x1924b8561eeF20e70Ede628A296175D358BE80e5",  # builder wallet (Based)
    "f": 100,  # fee en décimos de bps (100 = 10 bps = 0.1%)
}
BASED_CLOID = "0xba5ed11067f2cc08ba5ed10000ba5ed1"  # client id

SPOT_MIN_NOTIONAL = 10.0  # ~10 USDC mínimo en spot (según docs)

# ----------------- Config HL -----------------
@dataclass
class HLConfig:
    private_key: Optional[str]           # ← ahora aceptamos None si usamos agente
    use_testnet: bool = False
    use_agent: bool = False
    agent_private_key: Optional[str] = None


class ExchangeAdapter:
    """
    Envoltorio de Info/Exchange (SDK oficial) + utilidades
    - conecta mainnet/testnet
    - resuelve el 'coin' spot
    - helpers de balances / place_limit / cancel
    - AJUSTE de lot/tick size según spot_meta
    """
    def __init__(self, cfg: HLConfig):
        self.cfg = cfg
        self.base_url = constants.TESTNET_API_URL if cfg.use_testnet else constants.MAINNET_API_URL

        self.info = Info(self.base_url, skip_ws=True)

        if cfg.use_agent and cfg.agent_private_key:
            # Modo agente: firmamos con la clave del agente y actuamos en nombre del owner (cfg.private_key)
            main_acct = Account.from_key(cfg.private_key) if cfg.private_key else None
            agent_acct = Account.from_key(cfg.agent_private_key)
            self.exchange = Exchange(agent_acct, self.base_url, account_address=(main_acct.address if main_acct else None))
            self.address = (main_acct.address if main_acct else "(owner: none)") + f" agent={agent_acct.address}"
            log.info(f"[HL] agent mode owner={getattr(main_acct,'address',None)} agent={agent_acct.address}")
        else:
            # Modo owner directo (single user). Requiere cfg.private_key.
            if not cfg.private_key:
                raise ValueError("Owner mode: falta private_key")
            acct = Account.from_key(cfg.private_key)
            self.exchange = Exchange(acct, self.base_url)
            self.address = acct.address
            log.info(f"[HL] main wallet {self.address}")

    # ---------- Resolver coin spot ----------
    def resolve_spot_coin(self, ticker: str) -> str:
        """
        Devuelve el identificador que espera HL para spot:
        - si ya viene '@123' lo devuelve
        - intenta mapear desde Info.spot_meta() (name -> @index o market name)
        - si no, devuelve el ticker tal cual (HL acepta NAME/USDC en varios casos)
        """
        if ticker.startswith("@"):
            return ticker

        target = ticker.upper().replace("/USD", "").replace("/USDC", "")
        try:
            sm = self.info.spot_meta()
            tokens = sm.get("tokens", [])
            universe = sm.get("universe", [])

            # (1) match por nombre exacto en tokens
            idx_for = {t["name"]: t["index"] for t in tokens if "name" in t and "index" in t}
            if target in idx_for:
                idx = idx_for[target]
                # encontrar nombre de mercado si existe
                for mkt in universe:
                    if idx in mkt.get("tokens", []):
                        return mkt.get("name", f"@{mkt.get('index', idx)}")
                return f"@{idx}"

            # (2) si vino completo 'NAME/USDC'
            base = ticker.split("/")[0].upper()
            if base in idx_for:
                idx = idx_for[base]
                for mkt in universe:
                    if idx in mkt.get("tokens", []):
                        return mkt.get("name", f"@{mkt.get('index', idx)}")
                return f"@{idx}"

        except Exception as e:
            log.warning(f"[HL] spot_meta fallback error: {e}")

        return ticker  # último recurso

    # ---------- helpers de meta: lot/tick ----------
    def _sz_decimals_for_market(self, coin: str) -> int:
        """
        Obtiene szDecimals del TOKEN BASE del mercado 'coin' (ej. @142 o 'UBTC/USDC').
        Si falla, default a 6.
        """
        try:
            sm = self.info.spot_meta()
            tokens = sm.get("tokens", [])
            universe = sm.get("universe", [])
            idx_to_sz = {}
            for t in tokens:
                szd = t.get("szDecimals", t.get("sz_decimals"))
                if szd is not None:
                    idx_to_sz[t["index"]] = int(szd)

            # localizar el mercado
            mkt = None
            if coin.startswith("@") and coin[1:].isdigit():
                want = int(coin[1:])
                for u in universe:
                    if u.get("index") == want or u.get("name") == coin:
                        mkt = u
                        break
            if mkt is None:
                for u in universe:
                    if u.get("name") == coin:
                        mkt = u
                        break
            if mkt is None and "/" in coin:
                base = coin.split("/")[0].upper()
                base_idx = None
                for t in tokens:
                    if t.get("name") == base:
                        base_idx = t["index"]
                        break
                if base_idx is not None:
                    for u in universe:
                        toks = u.get("tokens", [])
                        if toks and toks[0] == base_idx:
                            mkt = u
                            break

            if not mkt:
                return 6

            base_idx = mkt.get("tokens", [None, None])[0]
            return int(idx_to_sz.get(base_idx, 6))
        except Exception as e:
            log.warning(f"[HL] _sz_decimals_for_market error: {e}")
            return 6

    @staticmethod
    def best_bid_ask_from_orderbook(orderbook: Dict[str, list]) -> Tuple[Optional[float], Optional[float]]:
        try:
            bids = orderbook.get("bids", [])
            asks = orderbook.get("asks", [])
            def _px(e): return float(e["px"]) if isinstance(e, dict) else float(e[0])
            best_bid = _px(bids[0]) if bids else None
            best_ask = _px(asks[0]) if asks else None
            return best_bid, best_ask
        except Exception:
            return None, None

    # ---------- Balances ----------
    def balances(self) -> Dict[str, float]:
        out: Dict[str, float] = {}
        try:
            acct = self.exchange.account()
            for coin in acct.get("spot", {}).get("balances", []):
                out[coin["coin"]] = float(coin["total"])
        except Exception as e:
            log.warning(f"[HL] balances error: {e}")
        return out

    # ---------- Ordenes ----------
    def place_limit(self, coin: str, side: str, sz: float, px: float) -> Dict[str, Any]:
        """
        Envía una limit con builder de Based y AJUSTA a tick/lot size válidos:
        - size -> szDecimals del token base
        - price -> máximo decimales permitidos para spot (8 - szDecimals)
        - asegura notional >= 10 USDC

        Devuelve: {"status": "...", "oid": "...." (si resting), "raw": <respuesta entera>}
        """
        is_buy = side.lower() == "buy"
        sz_dec = self._sz_decimals_for_market(coin)
        px_dec = max(0, min(8 - sz_dec, 8))  # máx decimales precio

        # redondeos a la grilla
        step_sz = 10 ** (-sz_dec)
        size_q = max(step_sz, math.floor(sz / step_sz) * step_sz)  # truncar a paso válido
        limit_px = float(f"{px:.{px_dec}f}")

        # notional mínimo
        if limit_px * size_q < SPOT_MIN_NOTIONAL:
            size_needed = (SPOT_MIN_NOTIONAL / max(limit_px, 1e-9))
            mult = math.ceil(size_needed / step_sz)
            size_q = mult * step_sz

        size_q = float(f"{size_q:.8f}")

        order_params = {"limit": {"tif": "Gtc"}, "cloid": BASED_CLOID}

        log.info(f"[ORDER] {side.upper()} {size_q} {coin} @ {limit_px} (sz_dec={sz_dec}, px_dec={px_dec})")
        try:
            res = self.exchange.order(
                name=coin,
                is_buy=is_buy,
                sz=size_q,
                limit_px=limit_px,
                order_type=order_params,
                builder=BASED_BUILDER,
            )

            # extraer OID si quedó resting
            oid = None
            try:
                statuses = (res or {}).get("response", {}).get("data", {}).get("statuses", []) or []
                for st in statuses:
                    resting = st.get("resting") if isinstance(st, dict) else None
                    if resting and "oid" in resting:
                        oid = str(resting["oid"])
                        break
            except Exception:
                oid = None

            log.info(f"[RES] {res}")
            return {"status": (res or {}).get("status", "unknown"), "oid": oid, "raw": res}
        except Exception as e:
            log.error(f"[ORDER-EXC] {e}")
            return {"status": "error", "exception": str(e), "oid": None, "raw": None}

    def cancel(self, coin: str, oid: str) -> Dict[str, Any]:
        try:
            res = self.exchange.cancel(coin, oid)
            log.info(f"[CANCEL] oid={oid} -> {res}")
            return res
        except Exception as e:
            log.error(f"[CANCEL-EXC] {e}")
            return {"status": "error", "exception": str(e)}
