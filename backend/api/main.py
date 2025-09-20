


# backend/api/main.py
from fastapi import Request
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Optional, Dict
import os, uuid, time

from api import pidguard

# cargar .env
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from src.adapter import HLConfig, ExchangeAdapter  # ExchangeAdapter no se usa aquí, pero lo dejamos por compat
from src.maker_bot import BotArgs
from api.bot_manager import registry

# crypto / HL
from eth_account import Account
from eth_account.messages import encode_defunct
from hyperliquid.info import Info
from hyperliquid.utils import constants

import os
import httpx
from fastapi import Query
from fastapi.responses import JSONResponse

LIQD_WORKER_URL = os.getenv("LIQD_WORKER_URL", "").strip()

app = FastAPI(title="hl-maker-webapi", version="0.6")



# ---- CORS
ALLOW_ORIGINS = (os.getenv("ALLOW_ORIGINS") or "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ALLOW_ORIGINS if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.options("/{rest_of_path:path}")
def options_catch_all(rest_of_path: str):
    return Response(status_code=204)

# Limpia pidfiles de bots muertos al levantar el server
pidguard.cleanup_dead()

# ---- modelos
class StartReq(BaseModel):
    ticker: str = Field("UBTC/USDC")
    amount_per_level: float = Field(10.0, gt=0)
    min_spread: float = Field(0.1, ge=0)
    ttl: float = Field(20, ge=0)
    maker_only: bool = True
    testnet: bool = False
    # agent key viene desde el cliente (opcional). Si viene, se usa; no se guarda.
    use_agent: bool = True
    agent_private_key: Optional[str] = None

class NonceReq(BaseModel):
    address: str

class VerifyReq(BaseModel):
    address: str
    signature: str

class StopByTokenReq(BaseModel):
    token: str

# ---- memoria de sesión (sin claves)
NONCES: Dict[str, str] = {}     # address_lower -> nonce
SESSIONS: Dict[str, dict] = {}  # token -> {address, created_at}

# ---- helpers
def _get_server_key() -> Optional[str]:
    priv = (os.getenv("HL_PRIVATE_KEY") or "").strip()
    return priv or None

def _address_from_auth(authorization: str) -> Optional[str]:
    """
    Extrae la address del usuario (de SESSIONS) a partir del header Authorization: Bearer <token>.
    """
    if not authorization:
        return None
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        tok = parts[1]
        sess = SESSIONS.get(tok)
        if sess and "address" in sess:
            return str(sess["address"]).lower()
    return None

def _reg_key_from_auth(authorization: str) -> str:
    """
    Usamos un token por usuario para enrutar su bot/logs.
    Si no hay token, caemos en 'owner' (modo single user).
    """
    if not authorization:
        return "owner"
    parts = authorization.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        tok = parts[1]
        if tok in SESSIONS:
            return f"user:{tok}"
    return "owner"

def _build_cfg(req: StartReq, owner_addr: Optional[str]) -> HLConfig:
    """
    Construye HLConfig:
      - Si use_agent + agent_private_key, NO exigimos HL_PRIVATE_KEY.
        En ese caso, si no hay HL_PRIVATE_KEY necesitamos owner_addr (wallet del user).
      - Si NO use_agent, sí exigimos HL_PRIVATE_KEY (modo owner).
    """
    agent_key = (req.agent_private_key or "").strip() if req.use_agent else ""
    server_key = _get_server_key()

    # Validaciones
    if agent_key:
        # Modo agente
        if not server_key and not owner_addr:
            # Multiusuario puro: necesitamos el address del usuario autenticado
            raise HTTPException(
                status_code=400,
                detail="Falta address de usuario. Conectá la wallet (login) antes de iniciar el bot en modo agente."
            )
    else:
        # Modo owner: necesita server key
        if not server_key:
            raise HTTPException(
                status_code=400,
                detail="Falta HL_PRIVATE_KEY en server (o enviá agent_private_key y use_agent=true)."
            )

    return HLConfig(
        private_key=server_key,             # puede ser None si usamos agente
        use_testnet=req.testnet,
        use_agent=bool(agent_key),
        agent_private_key=agent_key or None,
        owner_address=owner_addr,           # <- importante para modo agente sin server key
    )

def _build_args(req: StartReq, cfg: HLConfig) -> BotArgs:
    return BotArgs(
        ticker=req.ticker,
        amount_per_level=req.amount_per_level,
        min_spread=req.min_spread,
        maker_only=req.maker_only,
        ttl=req.ttl,
        use_testnet=req.testnet,
        use_agent=cfg.use_agent,
        agent_private_key=cfg.agent_private_key,
    )

# ---- Auth con firma (NO crea agente ni guarda claves)
@app.post("/auth/nonce")
def auth_nonce(req: NonceReq):
    addr = (req.address or "").strip().lower()
    if not addr:
        raise HTTPException(status_code=400, detail="address requerido")
    nonce = uuid.uuid4().hex
    NONCES[addr] = nonce
    return {"nonce": nonce}

@app.post("/auth/verify")
def auth_verify(req: VerifyReq):
    addr = (req.address or "").strip().lower()
    sig  = (req.signature or "").strip()
    nonce = NONCES.get(addr)
    if not nonce:
        raise HTTPException(status_code=400, detail="nonce no encontrado; pedí /auth/nonce primero")

    msg = f"OperatorLiquid login\nAddress: {addr}\nNonce: {nonce}"
    try:
        recovered = Account.recover_message(encode_defunct(text=msg), signature=sig)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"firma inválida: {e}")

    if recovered.lower() != addr:
        raise HTTPException(status_code=400, detail="address no coincide con la firma")

    token = uuid.uuid4().hex
    SESSIONS[token] = {"address": addr, "created_at": int(time.time())}
    NONCES.pop(addr, None)
    return {"token": token}

# ---- bot
@app.post("/bot/start")
def start_bot(req: StartReq, authorization: str = Header(default="")):
    try:
        # Owner address del usuario autenticado (si hay token)
        owner_addr = _address_from_auth(authorization)

        # agent_private_key viene en el body si el user usa agente
        cfg = _build_cfg(req, owner_addr)
        args = _build_args(req, cfg)
        key = _reg_key_from_auth(authorization)

        registry.start(key, cfg, args)
        return {"ok": True, "using_agent": bool(cfg.use_agent), "key": key, "owner": owner_addr}
    except HTTPException:
        # Dejá que FastAPI devuelva el 4xx con su detalle
        raise
    except Exception as e:
        import logging, traceback
        logging.exception("start_bot failed")
        # Devolvemos 400 con el texto del error para debug rápido
        return Response(
            content=f'{{"ok":false,"error":"{type(e).__name__}: {str(e)}"}}',
            media_type="application/json",
            status_code=400
        )


@app.post("/bot/stop")
def stop_bot(authorization: str = Header(default="")):
    key = _reg_key_from_auth(authorization)
    registry.stop(key)
    return {"ok": True}

# Para cerrar pestaña / desconexión de wallet desde el front (sendBeacon)
@app.post("/bot/stop_by_token")
async def stop_by_token(req: Request):
    # Acepta application/json, text/plain, o token=... (sendBeacon)
    token = ""
    try:
        ct = (req.headers.get("content-type") or "").lower()
        if "application/json" in ct:
            data = await req.json()
            token = (data.get("token") or "").strip()
        else:
            raw = (await req.body()).decode("utf-8", "ignore").strip()
            if raw.startswith("{"):
                import json
                token = (json.loads(raw).get("token") or "").strip()
            elif "token=" in raw:
                import urllib.parse as up
                token = dict(up.parse_qsl(raw)).get("token", "").strip()
            else:
                token = raw
    except Exception:
        token = token or ""

    key = f"user:{token}" if token and token in SESSIONS else "owner"
    registry.stop(key)
    pidguard.kill_key(key)
    return {"ok": True}

# Martillo global por si queda algo vivo (opcional)
@app.post("/bot/stop_all")
def stop_all():
    pids = pidguard.kill_all()
    return {"ok": True, "killed": pids}

@app.get("/bot/status")
def status(authorization: str = Header(default="")):
    key = _reg_key_from_auth(authorization)
    br = registry.get(key)
    # ANTES: p = getattr(getattr(br, "state", None), "proc", None) if br else None
    p = getattr(br, "proc", None) if br else None
    running = bool(p and p.is_alive())
    pid = getattr(p, "pid", None)
    return {"running": running, "pid": pid, "key": key}

# --- /bot/debug ---
@app.get("/bot/debug")
def bot_debug(authorization: str = Header(default="")):
    import psutil  # ya lo tenés instalado
    key = _reg_key_from_auth(authorization)
    br = registry.get(key)

    # ANTES:
    # state = getattr(br, "state", None) if br else None
    # p = getattr(state, "proc", None) if state else None
    # pid = getattr(p, "pid", None)

    p = getattr(br, "proc", None) if br else None
    pid = getattr(p, "pid", None)
    alive = bool(p and p.is_alive())
    pidfile_pid = pidguard.read_pid(key)

    try:
        ps_status = psutil.Process(pid).status() if pid else None
    except Exception:
        ps_status = None

    try:
        last_logs = br.read_logs(80) if br else []
    except Exception:
        last_logs = []

    return {
        "key": key,
        "has_runner": bool(br),
        "pid": pid,
        "alive": alive,
        "pidfile_pid": pidfile_pid,
        "psutil_status": ps_status,
        "started_at": getattr(br, "started_at", None) if br else None,
        "last_beat": getattr(br, "last_beat", None) if br else None,
        "logs": last_logs,
    }


# ---- WS logs (token por query para multiusuario)
@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket):
    await ws.accept()
    tok = ws.query_params.get("token", "")
    key = f"user:{tok}" if tok and tok in SESSIONS else "owner"
    br = registry.get(key)
    try:
        while True:
            lines = br.read_logs(200) if br else ["(bot no iniciado)"]
            for ln in lines:
                await ws.send_text(ln)
            await ws.receive_text()
    except WebSocketDisconnect:
        pass

# ---- meta spot (sin depender del adapter)
@app.get("/meta/spot")
def spot_meta():
    use_testnet = (os.getenv("HL_USE_TESTNET", "false").lower() == "true")
    base_url = constants.TESTNET_API_URL if use_testnet else constants.MAINNET_API_URL
    info = Info(base_url, skip_ws=True)
    try:
        sm = info.spot_meta()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"spot_meta error: {e}")

    tokens = sm.get("tokens", []) or []
    universe = sm.get("universe", []) or []
    idx_to_name = {}
    for t in tokens:
        try:
            idx = t.get("index")
            name = t.get("name") or t.get("symbol") or t.get("base")
            if idx is not None and name:
                idx_to_name[int(idx)] = str(name).upper()
        except Exception:
            continue

    out = []
    for m in universe:
        try:
            idx = m.get("index")
            base = idx_to_name.get(int(idx)) if idx is not None else None
            if not base:
                base = str(m.get("name","")).replace("@","").upper()
            out.append({
                "id": f"@{idx}" if idx is not None else (m.get("name") or base),
                "name": base, "base": base, "quote": "USDC"
            })
        except Exception:
            continue
    return {"ok": True, "meta": out}

# === LIQD token list proxy (HTTP/2 + headers tipo navegador + fallbacks) ===
@app.get("/liqd/recent_proxy")
def liqd_recent_proxy(
    limit: int = Query(24, ge=1, le=200),
    metadata: bool = True,
    search: str | None = Query(default=None),
):
    """
    Llama al Cloudflare Worker (relay) que a su vez consulta https://api.liqd.ag/tokens.
    Devuelve siempre {"tokens":[...]} o {"tokens":[],"error":"..."} (status 200).
    """
    if not LIQD_WORKER_URL:
        return JSONResponse(content={"tokens": [], "error": "worker_url_not_set"}, status_code=200)

    params = {"limit": str(limit), "metadata": "true" if metadata else "false"}
    if search:
        params["search"] = search

    try:
      with httpx.Client(timeout=8.0) as client:
          r = client.get(LIQD_WORKER_URL, params=params)
          r.raise_for_status()
          data = r.json()
          tokens = data.get("tokens") if isinstance(data, dict) else []
          if isinstance(tokens, list) and tokens:
              return JSONResponse(content={"tokens": tokens})
          return JSONResponse(content={"tokens": [], "error": data.get("error", "empty")}, status_code=200)
    except Exception as e:
      return JSONResponse(content={"tokens": [], "error": str(e)}, status_code=200)

      # === LIQD: recent unbonded (LiquidLaunch-only) ==============================
from typing import List
import json
from functools import lru_cache

# Web3 para leer estado en HyperEVM
try:
    from web3 import Web3
    from web3.exceptions import ContractLogicError
except Exception:
    Web3 = None  # si no está instalado, devolvemos un error amable más abajo

HYPER_RPC_URL = os.getenv("HYPER_RPC_URL", "https://rpc.hyperliquid.xyz/evm")
LL_CONTRACT_ADDR = Web3.to_checksum_address("0xDEC3540f5BA6f2aa3764583A9c29501FeB020030") if Web3 else "0xDEC3540f5BA6f2aa3764583A9c29501FeB020030"

# ABI mínimo: sólo lo que usamos
_LL_ABI = json.loads("""
[
  {
    "inputs":[{"internalType":"address","name":"token","type":"address"}],
    "name":"getTokenBondingStatus",
    "outputs":[
      {"internalType":"address","name":"tokenAddress","type":"address"},
      {"internalType":"bool","name":"isBonded","type":"bool"},
      {"internalType":"uint256","name":"bondedTimestamp","type":"uint256"}
    ],
    "stateMutability":"view","type":"function"
  },
  {
    "inputs":[{"internalType":"address","name":"token","type":"address"}],
    "name":"getTokenMetadata",
    "outputs":[
      {
        "components":[
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
        ],
        "internalType":"struct TokenMetadata","name":"","type":"tuple"
      }
    ],
    "stateMutability":"view","type":"function"
  }
]
""")

@lru_cache(maxsize=1)
def _w3_and_contract():
    if Web3 is None:
        raise HTTPException(status_code=500, detail="Falta dependencia web3 (pip install web3)")
    w3 = Web3(Web3.HTTPProvider(HYPER_RPC_URL, request_kwargs={"timeout": 8}))
    if not w3.is_connected():
        raise HTTPException(status_code=502, detail="No conecta al RPC de HyperEVM")
    c = w3.eth.contract(address=LL_CONTRACT_ADDR, abi=_LL_ABI)
    return w3, c

def _pick_tokens_shape(j) -> List[dict]:
    # normaliza formatos de api.liqd.ag
    if isinstance(j, list):
        return j
    if isinstance(j, dict):
        d = j.get("data") or {}
        if isinstance(d.get("tokens"), list):
            return d["tokens"]
        if isinstance(d.get("addresses"), list):
            return [{"address": a} for a in d["addresses"]]
        if isinstance(j.get("tokens"), list):
            return j["tokens"]
    return []

def _created_ms(meta) -> int:
    ts = meta.get("creationTimestamp", 0)
    try:
        ts = int(ts)
    except Exception:
        ts = 0
    # viene en segundos -> ms
    return ts * 1000 if ts < 10**12 else ts

@app.get("/liqd/recent_unbonded")
def liqd_recent_unbonded(limit: int = 24):
    """
    Devuelve hasta 'limit' tokens que:
      - existen en LiquidLaunch (getTokenMetadata no revierte)
      - NO están bonded (getTokenBondingStatus.isBonded == false)
    Ordenados del más nuevo (creationTimestamp) al más viejo.
    """
    # 1) Traemos semilla desde tu propio proxy si ya lo tenés, o directo si funciona
    seeds = []
    try:
      # intenta tu endpoint proxy primero (si lo tenés implementado)
      import requests
      r = requests.get(f"{os.getenv('PUBLIC_BASE_URL','https://api.liqd.ag')}/tokens?limit={limit*4}&metadata=true", timeout=6)
      if r.ok:
          seeds = _pick_tokens_shape(r.json())
    except Exception:
      seeds = []

    if not seeds:
        # si no hubo suerte, devolvemos vacío (frente estable) en vez de error
        return {"tokens": [], "count": 0}

    # 2) Lecturas on-chain
    try:
        w3, c = _w3_and_contract()
    except HTTPException as e:
        # no cortamos todo; devolvemos “unknown” claro
        return {"tokens": [], "count": 0, "error": e.detail}

    out = []
    for t in seeds:
        addr = (t.get("address") or t.get("token") or t.get("contract") or "").strip()
        if not addr:
            continue
        try:
            addr = Web3.to_checksum_address(addr)
        except Exception:
            continue

        # a) Confirma que sea token de LiquidLaunch (si revierte, no es)
        try:
            meta_tuple = c.functions.getTokenMetadata(addr).call()
            # reempaquetar en dict legible
            meta = {
              "name": meta_tuple[0], "symbol": meta_tuple[1],
              "image_uri": meta_tuple[2], "description": meta_tuple[3],
              "website": meta_tuple[4], "twitter": meta_tuple[5],
              "telegram": meta_tuple[6], "discord": meta_tuple[7],
              "creator": meta_tuple[8], "creationTimestamp": int(meta_tuple[9]),
              "startingLiquidity": int(meta_tuple[10]), "dexIndex": int(meta_tuple[11])
            }
        except ContractLogicError:
            # no es de LiquidLaunch -> skip
            continue
        except Exception:
            # cualquier otra falla (rpc, timeout) lo salteamos para no frenar todo
            continue

        # b) Estado de bonding
        try:
            _tAddr, isBonded, _bondTs = c.functions.getTokenBondingStatus(addr).call()
            if isBonded:
                continue  # queremos sólo NO bonded
        except Exception:
            continue

        out.append({
            "address": addr,
            "name": meta["name"],
            "symbol": meta["symbol"],
            "creationTimestamp": meta["creationTimestamp"],
            "dexIndex": meta["dexIndex"]
        })

        if len(out) >= limit:
            break

    # 3) ordenar del más nuevo al más viejo
    out.sort(key=lambda x: _created_ms(x), reverse=True)
    return {"tokens": out, "count": len(out)}
