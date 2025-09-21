# backend/api/liqd_routes.py
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import List, Literal
import httpx

from .rpc_util import get_contract, created_ms, recent_from_logs

router = APIRouter()

def _ok(payload: dict, code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=code)

@router.get("/liqd/rpc_health")
async def rpc_health():
    try:
        w3, _ = get_contract()
        return _ok({"connected": True, "blockNumber": int(w3.eth.block_number)})
    except Exception as e:
        return _ok({"connected": False, "error": str(e)}, 500)

@router.get("/liqd/recent_launch")
async def recent_launch(
    limit: int = Query(30, ge=1, le=200),
    bonded: Literal["unbonded","bonded","both"] = Query("unbonded"),
    page_size: int = Query(25, ge=5, le=150)   # <= tamaño paquetito
):
    """
    Intenta getPaginatedTokensWithMetadata; si falla el decode, fallback:
    - lees eventos TokenCreated recientes
    - por cada address, llama a getTokenMetadata(token) + estados
    """
    w3, c = get_contract()

    def _bonded_match(flag: bool) -> bool:
        if bonded == "both": return True
        return (flag and bonded == "bonded") or ((not flag) and bonded == "unbonded")

    tokens_out: List[dict] = []
    visited = 0

    # --------- camino 1: paginado nativo (rápido) ----------
    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        total = 0

    try:
        cursor = total
        while cursor > 0 and len(tokens_out) < limit:
            start = max(0, cursor - page_size)
            addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, cursor - start).call()
            visited += len(addrs)
            # recorrer de más nuevo a más viejo
            for i in range(len(addrs)-1, -1, -1):
                addr = addrs[i]
                meta = metas[i]
                # estados
                try:
                    _, isBonded, bts = c.functions.getTokenBondingStatus(addr).call()
                except Exception:
                    isBonded, bts = False, 0
                try:
                    _, isFrozen, fts = c.functions.getTokenFrozenStatus(addr).call()
                except Exception:
                    isFrozen, fts = False, 0
                if not _bonded_match(bool(isBonded)):
                    continue
                item = {
                    "address": addr,
                    "name": meta[0], "symbol": meta[1], "image": meta[2],
                    "creator": meta[8],
                    "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                    "startingLiquidity": str(meta[10]),
                    "dexIndex": int(meta[11]),
                    "isBonded": bool(isBonded),
                    "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
                    "isFrozen": bool(isFrozen),
                    "frozenTimestamp": int(fts) if str(fts).isdigit() else 0,
                }
                tokens_out.append(item)
                if len(tokens_out) >= limit:
                    break
            cursor = start
        # si logramos armar algo, devolvemos
        if tokens_out:
            tokens_out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
            return _ok({"tokens": tokens_out, "count": len(tokens_out),
                        "pageInfo": {"total": total, "visited": visited}})
    except Exception as e:
        # seguimos al fallback
        pass

    # --------- camino 2: fallback por eventos + getTokenMetadata() ----------
    addrs = recent_from_logs(w3, c, limit=max(limit*2, 40))
    for addr in addrs:
        try:
            meta = c.functions.getTokenMetadata(addr).call()
            try:
                _, isBonded, bts = c.functions.getTokenBondingStatus(addr).call()
            except Exception:
                isBonded, bts = False, 0
            try:
                _, isFrozen, fts = c.functions.getTokenFrozenStatus(addr).call()
            except Exception:
                isFrozen, fts = False, 0
            if not _bonded_match(bool(isBonded)):
                continue
            item = {
                "address": addr,
                "name": meta[0], "symbol": meta[1], "image": meta[2],
                "creator": meta[8],
                "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                "startingLiquidity": str(meta[10]),
                "dexIndex": int(meta[11]),
                "isBonded": bool(isBonded),
                "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
                "isFrozen": bool(isFrozen),
                "frozenTimestamp": int(fts) if str(fts).isdigit() else 0,
            }
            tokens_out.append(item)
            if len(tokens_out) >= limit:
                break
        except Exception:
            continue

    tokens_out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
    return _ok({"tokens": tokens_out[:limit], "count": len(tokens_out[:limit]),
                "pageInfo": {"mode": "fallback"}})

@router.get("/liqd/recent_unbonded_chain")
async def recent_unbonded_chain(limit: int = Query(30, ge=1, le=200), page_size: int = Query(25, ge=5, le=150)):
    return await recent_launch(limit=limit, bonded="unbonded", page_size=page_size)

@router.get("/liqd/recent_frozen")
async def recent_frozen(limit: int = Query(24, ge=1, le=200)):
    w3, c = get_contract()
    out: List[dict] = []
    addrs = recent_from_logs(w3, c, limit=200)
    for addr in addrs:
        try:
            _, frozen, fts = c.functions.getTokenFrozenStatus(addr).call()
            if not frozen:
                continue
            meta = c.functions.getTokenMetadata(addr).call()
            out.append({
                "address": addr,
                "name": meta[0], "symbol": meta[1], "image": meta[2],
                "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                "isFrozen": True, "frozenTimestamp": int(fts) if str(fts).isdigit() else 0
            })
            if len(out) >= limit:
                break
        except Exception:
            continue
    out.sort(key=lambda x: x.get("frozenTimestamp", 0), reverse=True)
    return _ok({"tokens": out, "count": len(out)})
# backend/api/liqd_routes.py
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from typing import List, Literal
import httpx

from .rpc_util import get_contract, created_ms, recent_from_logs

router = APIRouter()

def _ok(payload: dict, code: int = 200) -> JSONResponse:
    return JSONResponse(payload, status_code=code)

@router.get("/liqd/rpc_health")
async def rpc_health():
    try:
        w3, _ = get_contract()
        return _ok({"connected": True, "blockNumber": int(w3.eth.block_number)})
    except Exception as e:
        return _ok({"connected": False, "error": str(e)}, 500)

@router.get("/liqd/recent_launch")
async def recent_launch(
    limit: int = Query(30, ge=1, le=200),
    bonded: Literal["unbonded","bonded","both"] = Query("unbonded"),
    page_size: int = Query(25, ge=5, le=150)   # <= tamaño paquetito
):
    """
    Intenta getPaginatedTokensWithMetadata; si falla el decode, fallback:
    - lee eventos TokenCreated recientes
    - por cada address, llama a getTokenMetadata(token) + estados
    """
    w3, c = get_contract()

    def _bonded_match(flag: bool) -> bool:
        if bonded == "both": return True
        return (flag and bonded == "bonded") or ((not flag) and bonded == "unbonded")

    tokens_out: List[dict] = []
    visited = 0

    # --------- camino 1: paginado nativo (rápido) ----------
    try:
        total = int(c.functions.getTokenCount().call())
    except Exception as e:
        total = 0

    try:
        cursor = total
        while cursor > 0 and len(tokens_out) < limit:
            start = max(0, cursor - page_size)
            addrs, metas = c.functions.getPaginatedTokensWithMetadata(start, cursor - start).call()
            visited += len(addrs)
            # recorrer de más nuevo a más viejo
            for i in range(len(addrs)-1, -1, -1):
                addr = addrs[i]
                meta = metas[i]
                # estados
                try:
                    _, isBonded, bts = c.functions.getTokenBondingStatus(addr).call()
                except Exception:
                    isBonded, bts = False, 0
                try:
                    _, isFrozen, fts = c.functions.getTokenFrozenStatus(addr).call()
                except Exception:
                    isFrozen, fts = False, 0
                if not _bonded_match(bool(isBonded)):
                    continue
                item = {
                    "address": addr,
                    "name": meta[0], "symbol": meta[1], "image": meta[2],
                    "creator": meta[8],
                    "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                    "startingLiquidity": str(meta[10]),
                    "dexIndex": int(meta[11]),
                    "isBonded": bool(isBonded),
                    "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
                    "isFrozen": bool(isFrozen),
                    "frozenTimestamp": int(fts) if str(fts).isdigit() else 0,
                }
                tokens_out.append(item)
                if len(tokens_out) >= limit:
                    break
            cursor = start
        # si logramos armar algo, devolvemos
        if tokens_out:
            tokens_out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
            return _ok({"tokens": tokens_out, "count": len(tokens_out),
                        "pageInfo": {"total": total, "visited": visited}})
    except Exception as e:
        # seguimos al fallback
        pass

    # --------- camino 2: fallback por eventos + getTokenMetadata() ----------
    addrs = recent_from_logs(w3, c, limit=max(limit*2, 40))
    for addr in addrs:
        try:
            meta = c.functions.getTokenMetadata(addr).call()
            try:
                _, isBonded, bts = c.functions.getTokenBondingStatus(addr).call()
            except Exception:
                isBonded, bts = False, 0
            try:
                _, isFrozen, fts = c.functions.getTokenFrozenStatus(addr).call()
            except Exception:
                isFrozen, fts = False, 0
            if not _bonded_match(bool(isBonded)):
                continue
            item = {
                "address": addr,
                "name": meta[0], "symbol": meta[1], "image": meta[2],
                "creator": meta[8],
                "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                "startingLiquidity": str(meta[10]),
                "dexIndex": int(meta[11]),
                "isBonded": bool(isBonded),
                "bondedTimestamp": int(bts) if str(bts).isdigit() else 0,
                "isFrozen": bool(isFrozen),
                "frozenTimestamp": int(fts) if str(fts).isdigit() else 0,
            }
            tokens_out.append(item)
            if len(tokens_out) >= limit:
                break
        except Exception:
            continue

    tokens_out.sort(key=lambda x: x.get("creationTimestamp", 0), reverse=True)
    return _ok({"tokens": tokens_out[:limit], "count": len(tokens_out[:limit]),
                "pageInfo": {"mode": "fallback"}})

@router.get("/liqd/recent_unbonded_chain")
async def recent_unbonded_chain(limit: int = Query(30, ge=1, le=200), page_size: int = Query(25, ge=5, le=150)):
    return await recent_launch(limit=limit, bonded="unbonded", page_size=page_size)

@router.get("/liqd/recent_frozen")
async def recent_frozen(limit: int = Query(24, ge=1, le=200)):
    w3, c = get_contract()
    out: List[dict] = []
    addrs = recent_from_logs(w3, c, limit=200)
    for addr in addrs:
        try:
            _, frozen, fts = c.functions.getTokenFrozenStatus(addr).call()
            if not frozen:
                continue
            meta = c.functions.getTokenMetadata(addr).call()
            out.append({
                "address": addr,
                "name": meta[0], "symbol": meta[1], "image": meta[2],
                "creationTimestamp": int(meta[9]) if str(meta[9]).isdigit() else 0,
                "isFrozen": True, "frozenTimestamp": int(fts) if str(fts).isdigit() else 0
            })
            if len(out) >= limit:
                break
        except Exception:
            continue
    out.sort(key=lambda x: x.get("frozenTimestamp", 0), reverse=True)
    return _ok({"tokens": out, "count": len(out)})
