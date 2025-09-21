# backend/api/rpc_util.py
import os
from typing import Tuple, List

from web3 import Web3
from web3.middleware import geth_poa_middleware

# Pool de RPCs (podés setear HYPER_RPC_URL / HYPER_RPC_URL_2)
RPC_POOL: List[str] = [
    (os.getenv("HYPER_RPC_URL") or "").strip(),
    "https://rpc.hyperliquid.xyz/evm",
    (os.getenv("HYPER_RPC_URL_2") or "").strip(),
    "https://hyperliquid.drpc.org",
]
RPC_POOL = [r for r in RPC_POOL if r]

LIQUIDLAUNCH_ADDR = Web3.to_checksum_address("0xDEC3540f5BA6f2aa3764583A9c29501FeB020030")  # docs
# ABI mínima (funciones que usamos)
LIQUIDLAUNCH_ABI = [
  {"type":"function","name":"getTokenCount","inputs":[],"outputs":[{"name":"","type":"uint256"}],"stateMutability":"view"},
  {"type":"function","name":"getPaginatedTokensWithMetadata","inputs":[{"name":"start","type":"uint256"},{"name":"limit","type":"uint256"}],
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
   ],
   "stateMutability":"view"
  },
  {"type":"function","name":"getTokenBondingStatus","inputs":[{"name":"token","type":"address"}],
   "outputs":[
     {"name":"tokenAddress","type":"address"},
     {"name":"isBonded","type":"bool"},
     {"name":"bondedTimestamp","type":"uint256"}
   ],"stateMutability":"view"
  },
  {"type":"function","name":"getTokenFrozenStatus","inputs":[{"name":"token","type":"address"}],
   "outputs":[
     {"name":"tokenAddress","type":"address"},
     {"name":"isFrozen","type":"bool"},
     {"name":"frozenTimestamp","type":"uint256"}
   ],"stateMutability":"view"
  },
]

def choose_w3(rpc: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
    # HyperEVM puede requerir compatibilidad POA
    try:
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)
    except Exception:
        pass
    return w3

def _first_w3() -> Web3:
    last_err = None
    for rpc in RPC_POOL:
        try:
            w3 = choose_w3(rpc)
            if w3.is_connected():
                return w3
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"No RPC reachable: {last_err or 'unknown'}")

def get_contract() -> Tuple[Web3, any]:
    w3 = _first_w3()
    c = w3.eth.contract(address=LIQUIDLAUNCH_ADDR, abi=LIQUIDLAUNCH_ABI)
    return w3, c

def created_ms(item: dict) -> int:
    """
    Normaliza timestamps varios → milisegundos.
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
