# backend/api/liqd_routes.py
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import List
import os
import httpx

from .rpc_util import get_contract, created_ms

router = APIRouter()

# Opcional: relay para LIQD (Cloudflare Worker / Vercel)
LIQD_WORKER_URL = (os.getenv("LIQD_WORKER_URL") or "").strip()  # ej: https://tu-worker.workers.dev


@router.get("/liqd/recent_proxy")
def liqd_recent_proxy(
    limit: int = Query(24, ge=1, le=200),
    metadata: bool = True,
    search: str | None = Query(default=None),
):
    """
    Proxy de la token-list de LIQD.
    Devuelve {"tokens":[...]} o {"tokens":[],"error":"..."} (status 200).
    """
    params = {"limit": str(limit), "metadata": "true" if metadata else "false"}
    if search:
        params["search"] = search

    urls = []
    if LIQD_WORKER_URL:
        urls.append(LIQD_WORKER_URL)
    urls.append("https://api.liqd.ag/tokens")

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


@router.get("/liqd/recent_unbonded_chain")
def liqd_recent_unbonded_chain(
    limit: int = Query(24, ge=1, le=200),
    page_size: int = Query(100, ge=10, le=100),
):
    """
    On-chain puro:
      - getTokenCount()
      - getPaginatedTokensWithMetadata() y recorre hacia atrás,
      - getTokenBondingStatus(token).isBonded == false,
      - ordena por creationTimestamp desc.
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

    out = []
    remaining = limit
    while remaining > 0 and total > 0:
        start = max(0, total - page_size)
        size = min(page_size, total - start)  # <= importante
        try:
            tokens, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
        except Exception:
            break

        for i in reversed(range(len(tokens))):
            addr = tokens[i]
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

        total = start

    out.sort(key=created_ms, reverse=True)
    return JSONResponse({"tokens": out, "count": len(out)}, status_code=200)


@router.get("/liqd/recent_unbonded")
def liqd_recent_unbonded(limit: int = Query(24, ge=1, le=200)):
    """
    Semilla LIQD (o Worker) + validación on-chain:
      - getTokenMetadata(token) confirma que es de LiquidLaunch.
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
            "creationTimestamp": int(meta[9])
        })
        if len(out) >= limit:
            break

    out.sort(key=created_ms, reverse=True)
    return JSONResponse({"tokens": out, "count": len(out)}, status_code=200)


@router.get("/liqd/recent_frozen")
def liqd_recent_frozen(
    limit: int = Query(24, ge=1, le=200),
    page_size: int = Query(100, ge=10, le=100),  # <= acotado
):
    """
    Tokens LiquidLaunch con isFrozen=true (listos para migrar).
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
    size  = min(page_size, total - start)  # <= importante
    try:
        tokens, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getPaginated: {e}"}, status_code=200)

    out: List[dict] = []
    for i in reversed(range(len(tokens))):
        addr = tokens[i]
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


@router.get("/liqd/recent_tokens_rpc")
def liqd_recent_tokens_rpc(
    limit: int = Query(30, ge=1, le=200),
    bonded: str = Query("both", pattern="^(both|unbonded|bonded)$"),
    page_size: int = Query(100, ge=50, le=100),  # MAX 100
):
    """
    Lista recientes directo por RPC; bonded: both|unbonded|bonded
    """
    # conectar contrato
    try:
        w3, c = get_contract()
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return JSONResponse({"tokens": [], "count": 0, "error": e.detail}, status_code=200)
        return JSONResponse({"tokens": [], "count": 0, "error": str(e)}, status_code=200)

    # total
    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return JSONResponse({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"}, status_code=200)

    if total <= 0:
        return JSONResponse({"tokens": [], "count": 0}, status_code=200)

    # bloque desde el final
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

    out = []
    want_bonded = bonded != "unbonded"
    want_unbonded = bonded != "bonded"

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


@router.get("/liqd/_ll_debug")
def liqd_ll_debug(page_size: int = 50):
    """
    Debug del contrato LiquidLaunch:
      - chainId y blockNumber
      - getTokenCount()
      - muestra hasta page_size últimas direcciones + bondedStatus (no filtra)
    """
    try:
        w3, c = get_contract()
    except Exception as e:
        from fastapi import HTTPException
        if isinstance(e, HTTPException):
            return {"ok": False, "error": e.detail}
        return {"ok": False, "error": str(e)}

    info = {"ok": True, "rpc": str(w3.provider.endpoint_uri)}
    try:
        info["chainId"] = int(w3.eth.chain_id)
    except Exception as e:
        info["chainId_error"] = str(e)
    try:
        info["blockNumber"] = int(w3.eth.block_number)
    except Exception as e:
        info["blockNumber_error"] = str(e)

    try:
        total = int(c.functions.getTokenCount().call())
        info["tokenCount"] = total
    except Exception as e:
        info["tokenCount_error"] = str(e)
        return info

    if total <= 0:
        info["sample"] = []
        return info

    start = max(0, total - page_size)
    size  = min(page_size, total - start)
    sample = []
    try:
        addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
        for i in reversed(range(len(addrs))):
            a = addrs[i]
            try:
                _a, isBonded, ts = c.functions.getTokenBondingStatus(a).call()
            except Exception:
                isBonded, ts = None, None
            m = metas[i]
            sample.append({
                "address": a,
                "name": m[0],
                "symbol": m[1],
                "creationTimestamp": int(m[9]) if str(m[9]).isdigit() else m[9],
                "isBonded": isBonded,
                "bondedTimestamp": int(ts) if (ts is not None and str(ts).isdigit()) else ts,
            })
    except Exception as e:
        info["sample_error"] = str(e)
        sample = []

    info["sample_size"] = len(sample)
    info["sample"] = sample[: min(len(sample), 10)]
    return info
