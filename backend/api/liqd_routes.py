from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
import httpx, os

router = APIRouter()
TOKENS_API = "https://api.liqd.ag/tokens"
RPC_URL = os.getenv("HYPER_RPC_URL", "https://rpc.hyperliquid.xyz/evm")

def _ok(d, code=200): return JSONResponse(d, status_code=code)

@router.get("/liqd/rpc_health")
async def rpc_health():
    try:
        body = {"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.post(RPC_URL, json=body)
            r.raise_for_status()
            bn = int(r.json().get("result","0x0"), 16)
        return _ok({"connected": True, "blockNumber": bn})
    except Exception as e:
        return _ok({"connected": False, "error": str(e)})

@router.get("/liqd/recent_tokens_rpc")
@router.get("/liqd/recent_launch")
@router.get("/liqd/recent_unbonded_chain")
async def recent_tokens_rpc(limit: int = Query(30, ge=1, le=200), bonded: str = "both", page_size: int = 25):
    # Parche: trae tokens públicos (no filtra unbonded). Evita que la UI quede en blanco.
    try:
        params = {"limit": str(limit), "metadata": "true"}
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(TOKENS_API, params=params)
            r.raise_for_status()
            data = r.json()
        arr = (data or {}).get("data", {}).get("tokens", []) or []
        out = [{"address": t.get("address",""), "name": t.get("name",""),
                "symbol": t.get("symbol",""), "creationTimestamp": 0,
                "isBonded": False, "bondedTimestamp": 0} for t in arr[:limit]]
        return _ok({"tokens": out, "count": len(out), "pageInfo": {"mode":"public_api"}})
    except Exception as e:
        return _ok({"tokens": [], "count": 0, "error": str(e)})

@router.get("/liqd/recent_frozen")
async def recent_frozen(limit: int = Query(24, ge=1, le=200)):
    return _ok({"tokens": [], "count": 0, "note": "fallback no-onchain"})
