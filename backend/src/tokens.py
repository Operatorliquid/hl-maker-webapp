import json, os

def _json_path(fname: str) -> str | None:
    here = os.path.dirname(__file__)
    cands = [
        os.path.join(here, fname),                 # dentro de src/
        os.path.join(os.getcwd(), fname),          # raíz del proyecto
        os.path.join(os.getcwd(), "src", fname),   # por si corrés desde la raíz y lo pusiste en src
    ]
    for p in cands:
        if os.path.exists(p):
            return p
    return None

def load_token_mapping(network: str) -> dict:
    fname = "spot_tokens_detailed_mainnet.json" if network == "mainnet" else "spot_tokens_detailed_testnet.json"
    path = _json_path(fname)
    if not path:
        print(f"[WARN] No encontré {fname}. Busqué en: {os.getcwd()} y {os.path.dirname(__file__)}. Seguimos sin mapping (fallback spot_meta).")
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        print(f"[OK] Cargado mapping desde: {path}")
        return data
    except Exception as e:
        print(f"[WARN] No pude leer {path}: {e}. Fallback a spot_meta.")
        return {}

def resolve_token_id(symbol: str, mapping: dict, fallback_spot_meta_fn=None) -> str:
    sym = symbol.strip().upper()
    if sym.startswith("@"):
        return sym
    base = sym.split("/")[0]
    meta = mapping.get("mapping", {}).get(base) or mapping.get(base)
    if meta and "index" in meta:
        return f"@{meta['index']}"
    if fallback_spot_meta_fn:
        sm = fallback_spot_meta_fn()
        tokens = sm.get("tokens", sm) if isinstance(sm, dict) else sm
        if isinstance(tokens, dict) and "tokens" in tokens:
            tokens = tokens["tokens"]
        for tok in tokens:
            nm = tok.get("name", "")
            idx = tok.get("index")
            if idx is None:
                continue
            if nm == base or f"{nm}/USDC" == sym:
                return "PURR/USDC" if nm == "PURR" else f"@{idx}"
    raise ValueError(f"No pude mapear símbolo '{sym}'. Verificá nombres: spot_tokens_detailed_mainnet.json / testnet.json")
