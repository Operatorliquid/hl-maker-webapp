import requests
from fastapi import Query
from fastapi.responses import JSONResponse

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

# === LIQD recent proxy (CORS-safe + headers + fallbacks) ===
@app.get("/liqd/recent_proxy")
def liqd_recent_proxy(limit: int = Query(24, ge=1, le=200)):
    """
    Proxy a https://api.liqd.ag/tokens con headers explícitos.
    - Envía User-Agent y Accept para evitar 403 por filtros anti-bot.
    - Si 403/errores: intenta variantes de URL como fallback.
    - Siempre retorna JSON (en error: {"tokens": [], "error": "..."}).
    """
    headers = {
        "User-Agent": "OperatorLiquidBot/1.0 (+https://hl-maker-webapp-production.up.railway.app)",
        "Accept": "application/json",
        "Connection": "close",
    }

    bases = [
        ("https://api.liqd.ag/tokens", {"limit": limit}),
        # fallbacks por si el upstream exige otra ruta o ignora params:
        ("https://api.liqd.ag/tokens", None),
        ("https://api.liqd.ag/v2/tokens", {"limit": limit}),
        ("https://api.liqd.ag/v2/tokens", None),
    ]

    for url, params in bases:
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=6)
            # si 403, probá siguiente variante
            if resp.status_code == 403:
                continue
            resp.raise_for_status()
            data = resp.json()
            # normalizar: queremos un array bajo "tokens" si el upstream cambia el shape
            if isinstance(data, list):
                out = data[:limit]
            elif isinstance(data, dict):
                arr = data.get("tokens") or data.get("data") or data.get("items") or []
                if isinstance(arr, list):
                    out = arr[:limit]
                else:
                    out = []
            else:
                out = []
            return JSONResponse(content={"tokens": out})
        except Exception as e:
            # seguimos intentando siguiente variante
            last_err = str(e)

    # si nada funcionó:
    return JSONResponse(content={"tokens": [], "error": last_err if 'last_err' in locals() else "unknown"}, status_code=200)

