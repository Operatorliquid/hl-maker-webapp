from .config import DEC_PX, DEC_SZ

def round_px(x: float) -> float:
    return float(f"{x:.{DEC_PX}f}")

def round_sz(x: float) -> float:
    return float(f"{x:.{DEC_SZ}f}")

def bps(a: float, b: float) -> float:
    mid = (a + b) / 2.0
    if mid <= 0:
        return 0.0
    return abs(a - b) / mid * 1e4
