# backend/api/liqd_routes.py
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import List
import os
import httpx

from .rpc_util import get_contract, created_ms  # usa tu util existente

router = APIRouter()

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _worker_base() -> str:
    """
    Normaliza LIQD_WORKER_URL:
      - agrega https:// si falta
      - quita barra final
    """
    u = (os.getenv("LIQD_WORKER_URL") or "").strip()
    if not u:
        return ""
    if not (u.startswith("http://") or u.startswith("https://")):
        u = "https://" + u
    return u.rstrip("/")


# ------------------------------------------------------------
# Token list (LIQD) con proxy opcional al worker
# ------------------------------------------------------------
@router.get("/liqd/recent_proxy")
def liqd_recent_proxy(
    limit: int = Query(24, ge=1, le=200),
    metadata: bool = True,
    search: str | None = Query(default=None),
):
    """
    Proxy de la token-list de LIQD (docs: /liquidswap-integration/token-list).
    Devuelve {"tokens":[...]} o {"tokens":[],"error":"..."} (status 200).
    """
    params = {"limit": str(limit), "metadata": "true" if metadata else "false"}
    if search:
        params["search"] = search

    urls = []
    wb = _worker_base()
    if wb:
        urls.append(wb)  # relay (Cloudflare Worker / Vercel)
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


# ------------------------------------------------------------
# On-chain puro: unbonded (recorre páginas hacia atrás)
# ------------------------------------------------------------
@router.get("/liqd/recent_unbonded_chain")
def liqd_recent_unbonded_chain(
    limit: int = Query(24, ge=1, le=200),
    page_size: int = Query(100, ge=10, le=500),
):
    """
    On-chain puro contra LiquidLaunch:
      - getTokenCount()
      - getPaginatedTokensWithMetadata() (vamos de atrás hacia adelante)
      - getTokenBondingStatus(token).isBonded == false
      - orden por creationTimestamp desc.
    """
    try:
        w3, c = get_contract()
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)
        return JSONResponse({"tokens": [], "count": 0, "error": str(e)}, status_code=200)

    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"}, status_code=200)

    out: List[dict] = []
    remaining = limit

    while remaining > 0 and total > 0:
        start = max(0, total - page_size)
        size = total - start
        try:
            addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
        except Exception:
            break

        for i in reversed(range(len(addrs))):
            addr = addrs[i]
            meta = metas[i]
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

    out.sort(key=created_ms, reverse=True)
    return JSONResponse({"tokens": out, "count": len(out)}, status_code=200)


# ------------------------------------------------------------
# Semilla LIQD + verificación on-chain (unbonded)
# ------------------------------------------------------------
@router.get("/liqd/recent_unbonded")
def liqd_recent_unbonded(limit: int = Query(24, ge=1, le=200)):
    """
    Semilla LIQD (token-list) + verificación on-chain:
      - getTokenMetadata(token) confirma que pertenece al Launch.
      - getTokenBondingStatus(token).isBonded == false
      - orden por creationTimestamp desc.
    """
    # 1) Semilla
    seeds: List[dict] = []
    for u in filter(None, [_worker_base(), "https://api.liqd.ag/tokens"]):
        try:
            with httpx.Client(timeout=8.0) as client:
                r = client.get(u, params={"limit": str(limit * 5), "metadata": "true"})
                if r.status_code == 200:
                    data = r.json()
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
        w3, c = get_contract()
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)
        return JSONResponse({"tokens": [], "count": 0, "error": str(e)}, status_code=200)

    out: List[dict] = []
    for t in seeds:
        addr = (t.get("address") or t.get("token") or t.get("contract") or "").strip()
        if not addr:
            continue
        try:
            a = w3.to_checksum_address(addr)
        except Exception:
            a = addr

        try:
            meta = c.functions.getTokenMetadata(a).call()
        except Exception:
            continue

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

    out.sort(key=created_ms, reverse=True)
    return JSONResponse({"tokens": out, "count": len(out)}, status_code=200)


# ------------------------------------------------------------
# Frozen (isFrozen=true)
# ------------------------------------------------------------
@router.get("/liqd/recent_frozen")
def liqd_recent_frozen(
    limit: int = Query(24, ge=1, le=200),
    page_size: int = Query(200, ge=10, le=1000),
):
    """
    Tokens LiquidLaunch con isFrozen=true (listos para migrar/habilitar pool).
    """
    try:
        w3, c = get_contract()
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)
        return JSONResponse({"tokens": [], "count": 0, "error": str(e)}, status_code=200)

    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"}, status_code=200)

    if total <= 0:
        return JSONResponse({"tokens": [], "count": 0}, status_code=200)

    start = max(0, total - page_size)
    size  = total - start
    try:
        addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getPaginated: {e}"}, status_code=200)

    out: List[dict] = []
    for i in reversed(range(len(addrs))):
        addr = addrs[i]
        meta = metas[i]
        try:
            _a, isFrozen, fts = c.functions.getTokenFrozenStatus(addr).call()
        except Exception:
            continue
        if not isFrozen:
            continue
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


# ------------------------------------------------------------
# RPC health
# ------------------------------------------------------------
@router.get("/liqd/rpc_health")
def liqd_rpc_health():
    """
    Estado de los RPCs configurados (para debug).
    """
    try:
        from web3 import Web3 as _W3
    except Exception:
        from .rpc_util import HYPER_RPC_URLS
        return {"ok": False, "error": "web3_missing", "rpcs": HYPER_RPC_URLS}

    from .rpc_util import HYPER_RPC_URLS
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


# ------------------------------------------------------------
# Última página por RPC local (bonded/unbonded/both)
# ------------------------------------------------------------
@router.get("/liqd/recent_tokens_rpc")
def liqd_recent_tokens_rpc(
    limit: int = Query(30, ge=1, le=200),
    bonded: str = Query("both", pattern="^(both|unbonded|bonded)$"),
    page_size: int = Query(100, ge=50, le=100),  # contrato limita a 100 por llamada
):
    """
    Trae la última página del contrato (hasta 100), y aplica filtro:
      bonded = both | bonded | unbonded
    Devuelve {"tokens":[...], "count":N, "pageInfo":{...}}
    """
    try:
        w3, c = get_contract()
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)
        return JSONResponse({"tokens": [], "count": 0, "error": str(e)}, status_code=200)

    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"}, status_code=200)

    if total <= 0:
        return JSONResponse({"tokens": [], "count": 0, "pageInfo": {"total": 0, "start": 0, "size": 0, "returned": 0}}, status_code=200)

    start = max(0, total - page_size)
    size  = min(page_size, total - start)
    try:
        addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getPaginated: {e}"}, status_code=200)

    if not addrs:
        return JSONResponse({
            "tokens": [],
            "count": 0,
            "pageInfo": {"total": total, "start": start, "size": size, "returned": 0},
            "hint": "reduce page_size (max 100) o intenta otra página"
        }, status_code=200)

    want_bonded = bonded != "unbonded"
    want_unbonded = bonded != "bonded"
    out: List[dict] = []

    for i in reversed(range(len(addrs))):
        addr = addrs[i]
        meta = metas[i]
        try:
            _a, isBonded, bts = c.functions.getTokenBondingStatus(addr).call()
        except Exception:
            isBonded, bts = False, 0

        if (isBonded and not want_bonded) or ((not isBonded) and not want_unbonded):
            continue

        try:
            ct = int(meta[9]) if str(meta[9]).isdigit() else 0
        except Exception:
            ct = 0

        out.append({
            "address": addr,
            "name": meta[0],
            "symbol": meta[1],
            "creationTimestamp": ct,
            "isBonded": bool(isBonded),
            "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
        })
        if len(out) >= limit:
            break

    return JSONResponse({
        "tokens": out,
        "count": len(out),
        "pageInfo": {"total": total, "start": start, "size": size, "returned": len(addrs)}
    }, status_code=200)


# ------------------------------------------------------------
# Debug rápido del Worker
# ------------------------------------------------------------
@router.get("/liqd/worker_debug")
def liqd_worker_debug():
    base = _worker_base()
    if not base:
        return {"ok": False, "error": "worker_not_configured"}
    url = base + "/recent-tokens-rpc"
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get(url, params={"limit": "3", "bonded": "both"})
            return {
                "ok": r.status_code == 200,
                "status": r.status_code,
                "url": str(r.request.url),
                "content_type": r.headers.get("content-type"),
                "preview": r.text[:300]
            }
    except Exception as e:
        return {"ok": False, "url": url, "error": str(e)}


# ------------------------------------------------------------
# Worker proxy: recent tokens
# ------------------------------------------------------------
@router.get("/liqd/recent_tokens_worker")
def liqd_recent_tokens_worker(
    limit: int = Query(30, ge=1, le=200),
    bonded: str = Query("both", pattern="^(both|unbonded|bonded)$"),
):
    """
    Proxy al Cloudflare Worker: /recent-tokens-rpc
    Devuelve tal cual lo que responda el worker (status 200 siempre).
    """
    base = _worker_base()
    if not base:
        return JSONResponse({"tokens": [], "count": 0, "error": "worker_not_configured"}, status_code=200)

    url = base + "/recent-tokens-rpc"
    params = {"limit": str(limit), "bonded": bonded}
    try:
        with httpx.Client(timeout=8.0) as client:
            r = client.get(url, params=params)
            if r.status_code != 200:
                return JSONResponse({"tokens": [], "count": 0, "error": f"worker_{r.status_code}"}, status_code=200)
            data = r.json()
            # Normaliza posibles formas
            if isinstance(data, dict) and "tokens" in data:
                if "count" not in data:
                    try:
                        data["count"] = len(data.get("tokens") or [])
                    except Exception:
                        data["count"] = 0
                return JSONResponse(data, status_code=200)
            if isinstance(data, list):
                return JSONResponse({"tokens": data, "count": len(data)}, status_code=200)
            return JSONResponse({"tokens": [], "count": 0, "error": "worker_bad_shape"}, status_code=200)
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": str(e)}, status_code=200)

@router.get("/liqd/recent_launch")
def liqd_recent_launch(
    limit: int = Query(30, ge=1, le=300),
    page_size: int = Query(100, ge=50, le=100),  # el contrato devuelve máx 100
):
    """
    Lista reciente del *contrato* LiquidLaunch (solo Launch), mezclando UNBONDED + BONDED.
    Recorre páginas desde el final (más nuevos primero) hasta alcanzar `limit`.
    Devuelve: {"tokens":[{ address, name, symbol, creationTimestamp, isBonded, bondedTimestamp }], "count":N}
    """
    try:
        w3, c = get_contract()
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)
        return JSONResponse({"tokens": [], "count": 0, "error": str(e)}, status_code=200)

    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"}, status_code=200)

    if total <= 0:
        return JSONResponse({"tokens": [], "count": 0}, status_code=200)

    out = []
    remaining = limit
    cursor = total
    while remaining > 0 and cursor > 0:
        start = max(0, cursor - page_size)
        size = cursor - start
        try:
            addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
        except Exception as e:
            # si esta página falla, probamos la siguiente (más vieja)
            cursor = start
            continue

        # iteramos del más nuevo al más viejo
        for i in reversed(range(len(addrs))):
            addr = addrs[i]
            meta = metas[i]
            try:
                _a, isBonded, bts = c.functions.getTokenBondingStatus(addr).call()
            except Exception:
                isBonded, bts = False, 0
            try:
                ct = int(meta[9]) if str(meta[9]).isdigit() else 0
            except Exception:
                ct = 0

            out.append({
                "address": addr,
                "name": meta[0],
                "symbol": meta[1],
                "creationTimestamp": ct,
                "isBonded": bool(isBonded),
                "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
            })
            remaining -= 1
            if remaining <= 0:
                break

        cursor = start  # página anterior

    # más nuevos primero
    out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
    return JSONResponse({"tokens": out[:limit], "count": len(out[:limit])}, status_code=200)
