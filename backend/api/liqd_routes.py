# backend/api/liqd_routes.py
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import List, Literal, Optional
from .rpc_util import get_contract, choose_w3, created_ms, RPC_POOL

router = APIRouter()

def ok(data: dict, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)

@router.get("/liqd/rpc_health")
async def rpc_health():
    out = []
    for rpc in RPC_POOL:
        if not rpc: 
            continue
        w3 = choose_w3(rpc)
        connected = False
        block = None
        try:
            connected = bool(w3.is_connected())
            if connected:
                block = int(w3.eth.block_number)
        except Exception:
            connected = False
        out.append({"rpc": rpc, "connected": connected, "blockNumber": block})
    return ok({"rpcs": out})

def _bonded_match(flag: bool, desired: str) -> bool:
    # desired: 'unbonded' | 'bonded' | 'both'
    if desired == "both":
        return True
    return (flag and desired == "bonded") or ((not flag) and desired == "unbonded")

@router.get("/liqd/recent_launch")
async def recent_launch(
    limit: int = Query(30, ge=1, le=200),
    bonded: Literal["unbonded", "bonded", "both"] = Query("unbonded"),
    page_size: int = Query(100, ge=10, le=300)
):
    """
    Lee on-chain: getTokenCount + páginas de getPaginatedTokensWithMetadata,
    y por cada token consulta getTokenBondingStatus (y cong.)
    """
    w3, c = get_contract()
    # total tokens
    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return ok({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"})

    out: List[dict] = []
    visited = 0
    # iremos desde el final (recientes) hacia atrás por páginas
    cursor = total
    while cursor > 0 and len(out) < limit:
        start = max(0, cursor - page_size)
        try:
            addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, cursor - start).call()
        except Exception as e:
            return ok({"tokens": [], "count": 0, "error": f"getPaginatedTokensWithMetadata: {e}"})
        visited += len(addrs)

        # recorrer de más nuevo a más viejo dentro de la página
        for i in range(len(addrs)-1, -1, -1):
            addr = addrs[i]
            meta = metas[i]
            # meta: (name, symbol, image_uri, description, website, twitter, telegram, discord, creator, creationTimestamp, startingLiquidity, dexIndex)
            try:
                _, isBonded, bondedTs = c.functions.getTokenBondingStatus(addr).call()
            except Exception:
                isBonded, bondedTs = False, 0
            try:
                _, isFrozen, frozenTs = c.functions.getTokenFrozenStatus(addr).call()
            except Exception:
                isFrozen, frozenTs = False, 0

            if not _bonded_match(bool(isBonded), bonded):
                continue

            item = {
                "address": addr,
                "name": meta[0],
                "symbol": meta[1],
                "image": meta[2],
                "creator": meta[8],
                "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                "startingLiquidity": str(meta[10]),
                "dexIndex": int(meta[11]),
                "isBonded": bool(isBonded),
                "bondedTimestamp": int(bondedTs) if str(bondedTs).isdigit() else 0,
                "isFrozen": bool(isFrozen),
                "frozenTimestamp": int(frozenTs) if str(frozenTs).isdigit() else 0,
            }
            out.append(item)
            if len(out) >= limit:
                break

        cursor = start

    # Orden final (más nuevos primero)
    out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
    return ok({ "tokens": out, "count": len(out), "pageInfo": {"total": total, "visited": visited} })

@router.get("/liqd/recent_unbonded_chain")
async def recent_unbonded_chain(limit: int = Query(30, ge=1, le=200), page_size: int = Query(100, ge=10, le=300)):
    # azúcar sintáctico
    resp = await recent_launch(limit=limit, bonded="unbonded", page_size=page_size)
    return resp

@router.get("/liqd/recent_frozen")
async def recent_frozen(limit: int = Query(24, ge=1, le=200), page_size: int = Query(100, ge=10, le=300)):
    w3, c = get_contract()
    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        return ok({"tokens": [], "count": 0, "error": f"getTokenCount: {e}"})

    out: List[dict] = []
    cursor = total
    while cursor > 0 and len(out) < limit:
        start = max(0, cursor - page_size)
        try:
            addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, cursor - start).call()
        except Exception as e:
            return ok({"tokens": [], "count": 0, "error": f"getPaginatedTokensWithMetadata: {e}"})
        for i in range(len(addrs)-1, -1, -1):
            addr = addrs[i]
            meta = metas[i]
            try:
                _, isFrozen, frozenTs = c.functions.getTokenFrozenStatus(addr).call()
            except Exception:
                isFrozen, frozenTs = False, 0
            if not isFrozen:
                continue
            out.append({
                "address": addr,
                "name": meta[0],
                "symbol": meta[1],
                "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                "isFrozen": True,
                "frozenTimestamp": int(frozenTs) if str(frozenTs).isdigit() else 0,
            })
            if len(out) >= limit:
                break
        cursor = start

    out.sort(key=lambda x: x.get("frozenTimestamp", 0), reverse=True)
    return ok({ "tokens": out, "count": len(out) })

@router.get("/liqd/recent_tokens_rpc")
async def recent_tokens_rpc(
    limit: int = Query(30, ge=1, le=200),
    bonded: Literal["unbonded", "bonded", "both"] = Query("both"),
    page_size: int = Query(100, ge=10, le=300)
):
    # por compatibilidad con tu front, delega a recent_launch
    return await recent_launch(limit=limit, bonded=bonded, page_size=page_size)
