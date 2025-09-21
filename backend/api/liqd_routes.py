# backend/api/liqd_routes.py
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import List, Literal

# Import tardío para no crashear en import si falta web3
from .rpc_util import get_contract, created_ms, recent_from_logs

router = APIRouter()

def _ok(data: dict, code: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=code)

@router.get("/liqd/rpc_health")
async def rpc_health():
    try:
        w3, _ = get_contract()
        bn = int(w3.eth.block_number)
        return _ok({"connected": True, "blockNumber": bn})
    except Exception as e:
        # 200 con error → el front no cae
        return _ok({"connected": False, "error": str(e)})

def _want(bonded_flag: bool, mode: str) -> bool:
    if mode == "both": return True
    return (bonded_flag and mode == "bonded") or ((not bonded_flag) and mode == "unbonded")

@router.get("/liqd/recent_launch")
async def recent_launch(
    limit: int = Query(30, ge=1, le=200),
    bonded: Literal["unbonded","bonded","both"] = Query("unbonded"),
    page_size: int = Query(25, ge=5, le=150)
):
    """
    Camino A: getPaginatedTokensWithMetadata (rápido).
    Fallback: logs TokenCreated + getTokenMetadata + estados.
    NUNCA levanta excepción → devuelve tokens:[] en error.
    """
    try:
        w3, c = get_contract()
    except Exception as e:
        return _ok({"tokens": [], "count": 0, "error": str(e)})

    out: List[dict] = []
    # --------- Camino A ---------
    try:
        total = int(c.functions.getTokenCount().call())
        cursor = total
        while cursor > 0 and len(out) < limit:
            start = max(0, cursor - page_size)
            size = cursor - start
            addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, size).call()
            for i in range(len(addrs)-1, -1, -1):
                a = addrs[i]; m = metas[i]
                try:
                    _, isBonded, bts = c.functions.getTokenBondingStatus(a).call()
                except Exception:
                    isBonded, bts = False, 0
                try:
                    _, isFrozen, fts = c.functions.getTokenFrozenStatus(a).call()
                except Exception:
                    isFrozen, fts = False, 0
                if not _want(bool(isBonded), bonded):
                    continue
                out.append({
                    "address": a,
                    "name": m[0], "symbol": m[1], "image": m[2],
                    "creator": m[8],
                    "creationTimestamp": int(m[9]) if str(m[9]).isdigit() else 0,
                    "startingLiquidity": str(m[10]),
                    "dexIndex": int(m[11]),
                    "isBonded": bool(isBonded), "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
                    "isFrozen": bool(isFrozen), "frozenTimestamp": int(fts) if str(fts).isdigit() else 0,
                })
                if len(out) >= limit:
                    break
            cursor = start
        if out:
            out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
            return _ok({"tokens": out[:limit], "count": len(out[:limit]), "pageInfo": {"mode": "page"}})
    except Exception:
        # seguimos al fallback
        pass

    # --------- Fallback (logs + metadata por token) ---------
    try:
        addrs = recent_from_logs(w3, c, limit=max(2*limit, 60))
        for a in addrs:
            try:
                m = c.functions.getTokenMetadata(a).call()
                try:
                    _, isBonded, bts = c.functions.getTokenBondingStatus(a).call()
                except Exception:
                    isBonded, bts = False, 0
                try:
                    _, isFrozen, fts = c.functions.getTokenFrozenStatus(a).call()
                except Exception:
                    isFrozen, fts = False, 0
                if not _want(bool(isBonded), bonded):
                    continue
                out.append({
                    "address": a,
                    "name": m[0], "symbol": m[1], "image": m[2],
                    "creator": m[8],
                    "creationTimestamp": int(m[9]) if str(m[9]).isdigit() else 0,
                    "startingLiquidity": str(m[10]),
                    "dexIndex": int(m[11]),
                    "isBonded": bool(isBonded), "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
                    "isFrozen": bool(isFrozen), "frozenTimestamp": int(fts) if str(fts).isdigit() else 0,
                })
                if len(out) >= limit:
                    break
            except Exception:
                continue
        out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
        return _ok({"tokens": out[:limit], "count": len(out[:limit]), "pageInfo": {"mode": "logs"}})
    except Exception as e:
        return _ok({"tokens": [], "count": 0, "error": str(e)})

@router.get("/liqd/recent_unbonded_chain")
async def recent_unbonded_chain(limit: int = Query(30, ge=1, le=200), page_size: int = Query(25, ge=5, le=150)):
    return await recent_launch(limit=limit, bonded="unbonded", page_size=page_size)

@router.get("/liqd/recent_frozen")
async def recent_frozen(limit: int = Query(24, ge=1, le=200)):
    try:
        w3, c = get_contract()
    except Exception as e:
        return _ok({"tokens": [], "count": 0, "error": str(e)})
    out: List[dict] = []
    addrs = recent_from_logs(w3, c, limit=200)
    for a in addrs:
        try:
            _, frozen, fts = c.functions.getTokenFrozenStatus(a).call()
            if not frozen:
                continue
            m = c.functions.getTokenMetadata(a).call()
            out.append({
                "address": a,
                "name": m[0], "symbol": m[1], "image": m[2],
                "creationTimestamp": int(m[9]) if str(m[9]).isdigit() else 0,
                "isFrozen": True, "frozenTimestamp": int(fts) if str(fts).isdigit() else 0
            })
            if len(out) >= limit:
                break
        except Exception:
            continue
    out.sort(key=lambda x: x.get("frozenTimestamp", 0), reverse=True)
    return _ok({"tokens": out, "count": len(out)})

# Alias de compatibilidad con tu front:
@router.get("/liqd/recent_tokens_rpc")
async def recent_tokens_rpc(
    limit: int = Query(30, ge=1, le=200),
    bonded: Literal["unbonded","bonded","both"] = Query("both"),
    page_size: int = Query(25, ge=5, le=150)
):
    return await recent_launch(limit=limit, bonded=bonded, page_size=page_size)
