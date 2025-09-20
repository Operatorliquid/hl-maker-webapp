# backend/api/rpc_util.py
from fastapi import HTTPException
from typing import List, Tuple
import os

try:
    from web3 import Web3
except Exception:
    Web3 = None  # si web3 no está instalado, devolvemos error amable

# Pool de RPCs (ambos opcionales por ENV, con defaults públicos)
_DEFAULT_RPC_POOL = [
    (os.getenv("HYPER_RPC_URL") or "").strip(),   # tu preferido
    "https://rpc.hyperliquid.xyz/evm",            # oficial
    (os.getenv("HYPER_RPC_URL_2") or "").strip(), # alternativo
    "https://hyperliquid.drpc.org",               # mirror público
]
HYPER_RPC_URLS = [u for u in _DEFAULT_RPC_POOL if u] or ["https://rpc.hyperliquid.xyz/evm"]

# Dirección del contrato LiquidLaunch (mainnet HyperEVM)
LL_ADDRESS_RAW = "0xDEC3540f5BA6f2aa3764583A9c29501FeB020030"

# ABI mínimo (getTokenCount, getPaginatedTokensWithMetadata, getTokenBondingStatus, getTokenMetadata, getTokenFrozenStatus)
_LL_ABI_MIN = [
  {"inputs":[],"name":"getTokenCount","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"uint256","name":"start","type":"uint256"},{"internalType":"uint256","name":"limit","type":"uint256"}],
   "name":"getPaginatedTokensWithMetadata",
   "outputs":[
     {"internalType":"address[]","name":"tokens","type":"address[]"},
     {"components":[
        {"internalType":"string","name":"name","type":"string"},
        {"internalType":"string","name":"symbol","type":"string"},
        {"internalType":"string","name":"image_uri","type":"string"},
        {"internalType":"string","name":"description","type":"string"},
        {"internalType":"string","name":"website","type":"string"},
        {"internalType":"string","name":"twitter","type":"string"},
        {"internalType":"string","name":"telegram","type":"string"},
        {"internalType":"string","name":"discord","type":"string"},
        {"internalType":"address","name":"creator","type":"address"},
        {"internalType":"uint256","name":"creationTimestamp","type":"uint256"},
        {"internalType":"uint256","name":"startingLiquidity","type":"uint256"},
        {"internalType":"uint8","name":"dexIndex","type":"uint8"}
     ],"internalType":"struct TokenMetadata[]","name":"metadata","type":"tuple[]"}
   ],
   "stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"address","name":"token","type":"address"}],
   "name":"getTokenBondingStatus",
   "outputs":[
     {"internalType":"address","name":"tokenAddress","type":"address"},
     {"internalType":"bool","name":"isBonded","type":"bool"},
     {"internalType":"uint256","name":"bondedTimestamp","type":"uint256"}
   ],
   "stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"address","name":"token","type":"address"}],
   "name":"getTokenMetadata",
   "outputs":[{"components":[
        {"internalType":"string","name":"name","type":"string"},
        {"internalType":"string","name":"symbol","type":"string"},
        {"internalType":"string","name":"image_uri","type":"string"},
        {"internalType":"string","name":"description","type":"string"},
        {"internalType":"string","name":"website","type":"string"},
        {"internalType":"string","name":"twitter","type":"string"},
        {"internalType":"string","name":"telegram","type":"string"},
        {"internalType":"string","name":"discord","type":"string"},
        {"internalType":"address","name":"creator","type":"address"},
        {"internalType":"uint256","name":"creationTimestamp","type":"uint256"},
        {"internalType":"uint256","name":"startingLiquidity","type":"uint256"},
        {"internalType":"uint8","name":"dexIndex","type":"uint8"}
   ],"internalType":"struct TokenMetadata","name":"","type":"tuple"}],
   "stateMutability":"view","type":"function"},
  {"inputs":[{"internalType":"address","name":"token","type":"address"}],
   "name":"getTokenFrozenStatus",
   "outputs":[
     {"internalType":"address","name":"tokenAddress","type":"address"},
     {"internalType":"bool","name":"isFrozen","type":"bool"},
     {"internalType":"uint256","name":"frozenTimestamp","type":"uint256"}
   ],
   "stateMutability":"view","type":"function"}
]

def get_contract() -> Tuple["Web3", "Contract"]:
    """
    Devuelve (w3, contrato) probando cada RPC del pool. Timeout de 15s por RPC.
    No pisa nada del bot. Levanta HTTP 502 si no se pudo conectar a ninguno.
    """
    if Web3 is None:
        raise HTTPException(status_code=500, detail="web3 no instalado (pip install web3)")
    last_err = None
    # normalizamos checksum una vez
    try:
        ll_addr = Web3.to_checksum_address(LL_ADDRESS_RAW)
    except Exception:
        ll_addr = LL_ADDRESS_RAW
    for rpc in HYPER_RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if not w3.is_connected():
                last_err = f"no_connect:{rpc}"
                continue
            c = w3.eth.contract(address=ll_addr, abi=_LL_ABI_MIN)
            return w3, c
        except Exception as e:
            last_err = f"{type(e).__name__}@{rpc}: {e}"
    raise HTTPException(status_code=502, detail=f"RPC HyperEVM no disponible ({last_err})")

def created_ms(item: dict) -> int:
    """
    Normaliza timestamps (creationTimestamp/created_at/createdAt) a milisegundos.
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
