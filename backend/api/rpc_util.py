# backend/api/rpc_util.py
from typing import List, Tuple
import os

# Import perezoso: que el import de web3 NUNCA tire abajo el proceso
try:
    from web3 import Web3
    try:
        from web3.middleware import geth_poa_middleware
    except Exception:
        geth_poa_middleware = None
except Exception:
    Web3 = None
    geth_poa_middleware = None

# Pool de RPCs (configurables)
HYPER_RPC_URLS: List[str] = [
    (os.getenv("HYPER_RPC_URL") or "").strip(),
    "https://rpc.hyperliquid.xyz/evm",
    (os.getenv("HYPER_RPC_URL_2") or "").strip(),
    "https://hyperliquid.drpc.org",
]
HYPER_RPC_URLS = [u for u in HYPER_RPC_URLS if u]

LL_ADDRESS_RAW = "0xDEC3540f5BA6f2aa3764583A9c29501FeB020030"

# ABI mínima (según docs)
LIQUIDLAUNCH_ABI = [
    {"type":"function","name":"getTokenCount","inputs":[],"outputs":[{"type":"uint256"}],"stateMutability":"view"},
    {"type":"function","name":"getPaginatedTokensWithMetadata","stateMutability":"view",
     "inputs":[{"name":"start","type":"uint256"},{"name":"limit","type":"uint256"}],
     "outputs":[
        {"name":"tokens","type":"address[]"},
        {"name":"metadata","type":"tuple[]","components":[
            {"name":"name","type":"string"},
            {"name":"symbol","type":"string"},
            {"name":"image_uri","type":"string"},
            {"name":"description","type":"string"},
            {"name":"website","type":"string"},
            {"name":"twitter","type":"string"},
            {"name":"telegram","type":"string"},
            {"name":"discord","type":"string"},
            {"name":"creator","type":"address"},
            {"name":"creationTimestamp","type":"uint256"},
            {"name":"startingLiquidity","type":"uint256"},
            {"name":"dexIndex","type":"uint8"}
        ]}
     ]},
    {"type":"function","name":"getTokenMetadata","stateMutability":"view",
     "inputs":[{"name":"token","type":"address"}],
     "outputs":[{"name":"","type":"tuple","components":[
        {"name":"name","type":"string"},
        {"name":"symbol","type":"string"},
        {"name":"image_uri","type":"string"},
        {"name":"description","type":"string"},
        {"name":"website","type":"string"},
        {"name":"twitter","type":"string"},
        {"name":"telegram","type":"string"},
        {"name":"discord","type":"string"},
        {"name":"creator","type":"address"},
        {"name":"creationTimestamp","type":"uint256"},
        {"name":"startingLiquidity","type":"uint256"},
        {"name":"dexIndex","type":"uint8"}
     ]}]},
    {"type":"function","name":"getTokenBondingStatus","stateMutability":"view",
     "inputs":[{"name":"token","type":"address"}],
     "outputs":[
        {"name":"tokenAddress","type":"address"},
        {"name":"isBonded","type":"bool"},
        {"name":"bondedTimestamp","type":"uint256"}
     ]},
    {"type":"function","name":"getTokenFrozenStatus","stateMutability":"view",
     "inputs":[{"name":"token","type":"address"}],
     "outputs":[
        {"name":"tokenAddress","type":"address"},
        {"name":"isFrozen","type":"bool"},
        {"name":"frozenTimestamp","type":"uint256"}
     ]},
    {"type":"event","name":"TokenCreated","inputs":[
        {"name":"token","type":"address","indexed":True},
        {"name":"creator","type":"address","indexed":True},
        {"name":"name","type":"string","indexed":False},
        {"name":"symbol","type":"string","indexed":False},
        {"name":"image_uri","type":"string","indexed":False},
        {"name":"description","type":"string","indexed":False},
        {"name":"website","type":"string","indexed":False},
        {"name":"twitter","type":"string","indexed":False},
        {"name":"telegram","type":"string","indexed":False},
        {"name":"discord","type":"string","indexed":False},
        {"name":"creationTimestamp","type":"uint256","indexed":False},
        {"name":"startingLiquidity","type":"uint256","indexed":False},
        {"name":"currentHypeReserves","type":"uint256","indexed":False},
        {"name":"currentTokenReserves","type":"uint256","indexed":False},
        {"name":"totalSupply","type":"uint256","indexed":False},
        {"name":"currentPrice","type":"uint256","indexed":False},
        {"name":"initialPurchaseAmount","type":"uint256","indexed":False}
    ]},
]

def _connect(rpc: str):
    if Web3 is None:
        return None
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 12}))
    try:
        if geth_poa_middleware:
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    except Exception:
        pass
    try:
        if not w3.is_connected():
            return None
    except Exception:
        return None
    return w3

def get_contract() -> Tuple["Web3", "Contract"]:
    """
    Devuelve (w3, contrato) o levanta Exception si no hay RPC.
    OJO: NO se llama al importar, sólo cuando el endpoint lo requiere.
    """
    last_err = None
    addr = None
    try:
        if Web3 is None:
            raise RuntimeError("web3 no instalado (pip install web3)")
        addr = Web3.to_checksum_address(LL_ADDRESS_RAW)
    except Exception:
        addr = LL_ADDRESS_RAW

    for rpc in HYPER_RPC_URLS:
        try:
            w3 = _connect(rpc)
            if not w3:
                continue
            c = w3.eth.contract(address=addr, abi=LIQUIDLAUNCH_ABI)
            return w3, c
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"No RPC reachable: {last_err or 'unknown'}")

def created_ms(item: dict) -> int:
    """
    Normaliza timestamps → milisegundos.
    """
    v = item.get("creationTimestamp") or item.get("created_at") or item.get("createdAt")
    if v is None:
        return 0
    try:
        n = int(v)
        return n if n > 10**12 else n * 1000
    except Exception:
        try:
            from datetime import datetime
            return int(datetime.fromisoformat(str(v).replace("Z","")).timestamp() * 1000)
        except Exception:
            return 0

def recent_from_logs(w3, c, limit: int = 60, blocks_back: int = 250_000) -> List[str]:
    """
    Fallback por eventos TokenCreated. NO rompe si el RPC limita rangos.
    """
    latest = 0
    try:
        latest = int(w3.eth.block_number)
    except Exception:
        return []
    frm = max(0, latest - blocks_back)
    addrs: List[str] = []
    try:
        logs = c.events.TokenCreated().get_logs(fromBlock=frm, toBlock=latest)
        logs.sort(key=lambda e: (e["blockNumber"], e["logIndex"]), reverse=True)
        seen = set()
        for ev in logs:
            a = ev["args"]["token"]
            if a in seen:
                continue
            addrs.append(a); seen.add(a)
            if len(addrs) >= limit:
                break
        return addrs
    except Exception:
        # RPC con límites de rango: partimos
        step = max(5000, blocks_back // 20)
        seen = set()
        for start in range(frm, latest + 1, step):
            end = min(latest, start + step - 1)
            try:
                part = c.events.TokenCreated().get_logs(fromBlock=start, toBlock=end)
                for ev in part:
                    a = ev["args"]["token"]
                    if a in seen:
                        continue
                    addrs.append(a); seen.add(a)
                    if len(addrs) >= limit:
                        return addrs
            except Exception:
                continue
        return addrs
