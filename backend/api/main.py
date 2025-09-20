# backend/api/main.py
from fastapi import Request, FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel, Field
from typing import Optional, Dict, List
from pathlib import Path
import os, uuid, time

from api import pidguard

# cargar .env
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

# HTTP cliente
import httpx

# Opcional: relay para LIQD (Cloudflare Worker / Vercel)
LIQD_WORKER_URL = (os.getenv("LIQD_WORKER_URL") or "").strip()  # ej: https://summer-sea-4071.josestratta4.workers.dev

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
        owner_addr = _address_from_auth(authorization)
        cfg = _build_cfg(req, owner_addr)
        args = _build_args(req, cfg)
        key = _reg_key_from_auth(authorization)

        registry.start(key, cfg, args)
        return {"ok": True, "using_agent": bool(cfg.use_agent), "key": key, "owner": owner_addr}
    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.exception("start_bot failed")
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

# ====================================================================
# === LIQD token list proxy (usa Worker si está configurado) =========
# ====================================================================
@app.get("/liqd/recent_proxy")
def liqd_recent_proxy(
    limit: int = Query(24, ge=1, le=200),
    metadata: bool = True,
    search: str | None = Query(default=None),
):
    """
    Pide tokens a LIQD:
      - Si LIQD_WORKER_URL está seteado, usamos ese relay (recomendado).
      - Si no, intentamos directo a https://api.liqd.ag/tokens
    Devuelve {"tokens":[...]} o {"tokens":[],"error":"..."} (status 200).
    """
    params = {"limit": str(limit), "metadata": "true" if metadata else "false"}
    if search:
        params["search"] = search

    urls = []
    if LIQD_WORKER_URL:
        urls.append(LIQD_WORKER_URL)  # relay
    urls.append("https://api.liqd.ag/tokens")  # directo

    last_err = "unknown"
    for u in urls:
        try:
            with httpx.Client(timeout=8.0) as client:
                r = client.get(u, params=params)
                if r.status_code != 200:
                    last_err = f"upstream_{r.status_code}"
                    continue
                data = r.json()
                tokens = []
                if isinstance(data, list):
                    tokens = data
                elif isinstance(data, dict):
                    if isinstance(data.get("tokens"), list):
                        tokens = data["tokens"]
                    elif isinstance(data.get("data"), dict):
                        dd = data["data"]
                        if isinstance(dd.get("tokens"), list):
                            tokens = dd["tokens"]
                        elif isinstance(dd.get("addresses"), list):
                            tokens = [{"address": a} for a in dd["addresses"]]
                if tokens:
                    return JSONResponse({"tokens": tokens})
                last_err = "empty"
        except Exception as e:
            last_err = str(e)

    return JSONResponse({"tokens": [], "error": last_err}, status_code=200)

# ====================================================================
# === LiquidLaunch: helpers y endpoints on-chain ======================
# ====================================================================
# Web3 (opcionalmente configurable)
try:
    from web3 import Web3
    from web3.exceptions import ContractLogicError
except Exception:
    Web3 = None  # si no está instalado, devolvemos error amable

# --- RPCs con fallback (primero ENV, luego oficiales y mirrors públicos) ---
_default_rpc_pool = [
    os.getenv("HYPER_RPC_URL", "").strip(),        # opcional: tu preferido
    "https://rpc.hyperliquid.xyz/evm",             # oficial
    os.getenv("HYPER_RPC_URL_2", "").strip(),      # opcional: segundo personalizado
    "https://hyperliquid.drpc.org",                # mirror público dRPC
]
HYPER_RPC_URLS = [u for u in _default_rpc_pool if u] or ["https://rpc.hyperliquid.xyz/evm"]

LL_ADDRESS_RAW = "0xDEC3540f5BA6f2aa3764583A9c29501FeB020030"
LL_CONTRACT_ADDR = None
if Web3 is not None:
    try:
        LL_CONTRACT_ADDR = Web3.to_checksum_address(LL_ADDRESS_RAW)
    except Exception:
        LL_CONTRACT_ADDR = LL_ADDRESS_RAW

# ABI mínimo para lo que usamos
_LL_ABI_MIN = [
  {
    "inputs": [],
    "name": "getTokenCount",
    "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [{"internalType":"uint256","name":"start","type":"uint256"},{"internalType":"uint256","name":"limit","type":"uint256"}],
    "name": "getPaginatedTokensWithMetadata",
    "outputs": [
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
    "stateMutability":"view",
    "type":"function"
  },
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
      ],"internalType":"struct TokenMetadata","name":"","type":"tuple"}
    ],
    "stateMutability":"view","type":"function"
  }
]

# --- agregar función de estado 'frozen' (no rompe nada de lo anterior) ---
_LL_ABI_MIN.append({
  "inputs":[{"internalType":"address","name":"token","type":"address"}],
  "name":"getTokenFrozenStatus",
  "outputs":[
    {"internalType":"address","name":"tokenAddress","type":"address"},
    {"internalType":"bool","name":"isFrozen","type":"bool"},
    {"internalType":"uint256","name":"frozenTimestamp","type":"uint256"}
  ],
  "stateMutability":"view","type":"function"
})

def _created_ms(item: dict) -> int:
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

def _get_ll_contract():
    """
    Intenta conectar contra un pool de RPCs (HYPER_RPC_URLS) con timeout de 15s.
    Devuelve (w3, contrato) o levanta HTTPException 502 con detalle del último error.
    """
    if Web3 is None:
        raise HTTPException(status_code=500, detail="web3 no instalado (pip install web3)")

    last_err = None
    for rpc in HYPER_RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            if not w3.is_connected():
                last_err = f"no_connect:{rpc}"
                continue
            c = w3.eth.contract(address=LL_CONTRACT_ADDR or LL_ADDRESS_RAW, abi=_LL_ABI_MIN)
            return w3, c
        except Exception as e:
            last_err = f"{type(e).__name__}@{rpc}: {e}"

    raise HTTPException(status_code=502, detail=f"RPC HyperEVM no disponible ({last_err})")

# ---- Endpoint: on-chain puro (no depende de api.liqd.ag)
@app.get("/liqd/recent_unbonded_chain")
def liqd_recent_unbonded_chain(
    limit: int = Query(24, ge=1, le=200),
    page_size: int = Query(100, ge=10, le=500),
):
    """
    Lee directamente del contrato LiquidLaunch:
      - getTokenCount() para saber cuántos hay,
      - getPaginatedTokensWithMetadata() y recorre hacia atrás,
      - filtra getTokenBondingStatus(token).isBonded == false,
      - ordena por creationTimestamp desc.
    """
    try:
        w3, c = _get_ll_contract()
    except HTTPException as e:
        return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)

    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"}, status_code=200)

    out = []
    remaining = limit
    # Recorremos bloques desde el final (más nuevos primero)
    while remaining > 0 and total > 0:
        start = max(0, total - page_size)
        size = total - start
        try:
            tokens, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
        except Exception:
            break

        # iterar de atrás hacia adelante dentro del bloque
        for i in reversed(range(len(tokens))):
            addr = tokens[i]
            meta = metas[i]
            # skip bonded
            try:
                _t, isBonded, _ts = c.functions.getTokenBondingStatus(addr).call()
                if isBonded:
                    continue
            except Exception:
                continue

            out.append({
                "address": addr,
                "name": meta[0],
                "symbol": meta[1],
                "creationTimestamp": int(meta[9]),
            })
            remaining -= 1
            if remaining <= 0:
                break

        total = start  # paso al bloque anterior

    out.sort(key=_created_ms, reverse=True)
    return JSONResponse({"tokens": out, "count": len(out)}, status_code=200)

# ---- Endpoint: mixto (semilla LIQD + filtro on-chain)
@app.get("/liqd/recent_unbonded")
def liqd_recent_unbonded(limit: int = Query(24, ge=1, le=200)):
    """
    Usa LIQD (o Worker) como semilla y filtra on-chain:
      - getTokenMetadata(token) para confirmar que es de LiquidLaunch.
      - getTokenBondingStatus(token).isBonded == false
      - Orden por creationTimestamp desc.
    """
    # 1) Semilla
    seeds: List[dict] = []
    for u in filter(None, [LIQD_WORKER_URL, "https://api.liqd.ag/tokens"]):
        try:
            with httpx.Client(timeout=8.0) as client:
                r = client.get(u, params={"limit": str(limit * 5), "metadata": "true"})
                if r.status_code == 200:
                    data = r.json()
                    # normalizar formatos
                    if isinstance(data, list):
                        seeds = data
                    elif isinstance(data, dict):
                        if isinstance(data.get("tokens"), list):
                            seeds = data["tokens"]
                        elif isinstance(data.get("data"), dict):
                            dd = data["data"]
                            if isinstance(dd.get("tokens"), list):
                                seeds = dd["tokens"]
                            elif isinstance(dd.get("addresses"), list):
                                seeds = [{"address": a} for a in dd["addresses"]]
                    if seeds:
                        break
        except Exception:
            continue

    if not seeds:
        return JSONResponse({"tokens": [], "count": 0, "error": "seed_empty"}, status_code=200)

    # 2) Filtro on-chain
    try:
        w3, c = _get_ll_contract()
    except HTTPException as e:
        return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)

    out: List[dict] = []
    for t in seeds:
        addr = (t.get("address") or t.get("token") or t.get("contract") or "").strip()
        if not addr:
            continue
        try:
            a = Web3.to_checksum_address(addr) if Web3 is not None else addr
        except Exception:
            continue

        # Confirmar que es LiquidLaunch
        try:
            meta = c.functions.getTokenMetadata(a).call()
        except Exception:
            continue

        # No bonded
        try:
            _ta, isBonded, _bt = c.functions.getTokenBondingStatus(a).call()
            if isBonded:
                continue
        except Exception:
            continue

        out.append({
            "address": a,
            "name": meta[0],
            "symbol": meta[1],
            "creationTimestamp": int(meta[9])  # seconds
        })
        if len(out) >= limit:
            break

    out.sort(key=_created_ms, reverse=True)
    return JSONResponse({"tokens": out, "count": len(out)}, status_code=200)

# ---- Endpoint: frozen (listos para migrar) ---------------------------
@app.get("/liqd/recent_frozen")
def liqd_recent_frozen(
    limit: int = Query(24, ge=1, le=200),
    page_size: int = Query(200, ge=10, le=1000),
):
    """
    Lista tokens de LiquidLaunch que están 'frozen' (listos para migrar).
    Trae el último bloque paginado (más nuevos primero) y filtra por isFrozen=true.
    Ordena por frozenTimestamp desc (más “frescos” arriba).
    """
    try:
        w3, c = _get_ll_contract()
    except HTTPException as e:
        return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)

    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"}, status_code=200)

    if total <= 0:
        return JSONResponse({"tokens": [], "count": 0}, status_code=200)

    start = max(0, total - page_size)
    size  = total - start
    try:
        tokens, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getPaginated: {e}"}, status_code=200)

    out: List[dict] = []
    # Recorremos del más nuevo al más viejo
    for i in reversed(range(len(tokens)))):
        addr = tokens[i]
        meta = metas[i]
        try:
            _a, isFrozen, fts = c.functions.getTokenFrozenStatus(addr).call()
        except Exception:
            continue
        if not isFrozen:
            continue
        # meta[0]=name, meta[1]=symbol, meta[9]=creationTimestamp (segundos)
        try:
            ct = int(meta[9]) if str(meta[9]).isdigit() else 0
        except Exception:
            ct = 0
        out.append({
            "address": addr,
            "name": meta[0],
            "symbol": meta[1],
            "creationTimestamp": ct,
            "frozenTimestamp": int(fts) if str(fts).isdigit() else 0,
        })
        if len(out) >= limit:
            break

    out.sort(key=lambda x: x.get("frozenTimestamp", 0), reverse=True)
    return JSONResponse({"tokens": out, "count": len(out)}, status_code=200)

# ---- Healthcheck de RPCs --------------------------------------------
@app.get("/liqd/rpc_health")
def liqd_rpc_health():
    """
    Devuelve el estado de conectividad a cada RPC del pool.
    Ej: {"ok": true, "rpcs":[{"rpc":"...","connected":true,"blockNumber":123}, ...]}
    """
    try:
        from web3 import Web3 as _W3
    except Exception:
        return {"ok": False, "error": "web3_missing", "rpcs": HYPER_RPC_URLS}

    results = []
    for rpc in HYPER_RPC_URLS:
        st = {"rpc": rpc, "connected": False, "blockNumber": None, "error": None}
        try:
            w3 = _W3(_W3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            st["connected"] = bool(w3.is_connected())
            if st["connected"]:
                try:
                    st["blockNumber"] = int(w3.eth.block_number)
                except Exception as e:
                    st["error"] = f"blockNumber:{e}"
        except Exception as e:
            st["error"] = str(e)
        results.append(st)
    any_ok = any(r["connected"] for r in results)
    return {"ok": any_ok, "rpcs": results}
