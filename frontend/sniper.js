// sniper.js
const BACKEND = localStorage.getItem("BACKEND_BASE") || "https://hl-maker-webapp-production.up.railway.app";
const LAST_ADDR_KEY = "hl_last_addr";
const TOKEN_KEY = "hl_session_token";

const $ = (s)=>document.querySelector(s);
const els = {
  backendUrl: $("#backendUrlSniper"),

  // wallet / panel
  walletBtn: $("#btnConnectSniper"),
  walletLabel: $("#walletLabelSniper"),
  mmIcon: $("#mmIconSniper"),
  side: $("#sidePanel"),
  sideBackdrop: $("#sideBackdrop"),
  sideClose: $("#sideClose"),
  panelWallet: $("#panelWallet"),
  panelStatus: $("#panelStatus"),
  panelDisconnect: $("#panelDisconnect"),

  // sniper (nuevos del backend)
  newList: $("#newList"),
  btnRefresh: $("#btnRefreshNew"),
  tokenInInput: $("#sn_tokenIn"),
  amountInput: $("#sn_amount"),
  slippageInput: $("#sn_slippage"),

  // recent (LiquidLaunch)
  recentSkeletons: $("#recentSkeletons"),
  recentList: $("#recentList"),

  // frozen (opcional)
  frozenSkeletons: $("#frozenSkeletons"),
  frozenList: $("#frozenList"),
  btnRefreshFrozen: $("#btnRefreshFrozen"),
};

/* ===================== DEBUG ===================== */
const DEBUG = true;
function dlog(...args){ if (DEBUG) console.debug("[sniper]", ...args); }

let debugPanel;
function ensureDebugPanel(){
  if (debugPanel || !DEBUG) return;
  debugPanel = document.createElement("div");
  debugPanel.id = "sniperDebugPanel";
  debugPanel.style.cssText = "position:fixed;right:10px;bottom:10px;z-index:9999;background:#0b1418;opacity:.95;color:#cfe8e2;font:12px/1.3 ui-monospace,monospace;border:1px solid #26333a;border-radius:8px;padding:8px 10px;max-width:380px;display:none;";
  document.body.appendChild(debugPanel);
}
function setDebugText(html){
  ensureDebugPanel();
  if (debugPanel) debugPanel.innerHTML = html;
}
function toggleDebugPanel(){
  ensureDebugPanel();
  if (!debugPanel) return;
  debugPanel.style.display = (debugPanel.style.display === "none" || !debugPanel.style.display) ? "block" : "none";
}
document.addEventListener("keydown", (e)=>{ if ((e.key||"").toLowerCase() === "d") toggleDebugPanel(); });

/* ===================== Session ===================== */
let SESSION_TOKEN = localStorage.getItem(TOKEN_KEY) || "";
let CONNECTED = !!SESSION_TOKEN;

function short(a){ return a ? (a.slice(0,6)+"…"+a.slice(-4)) : ""; }

async function api(path, opts = {}) {
  const hasBody = typeof opts.body !== "undefined";
  const headers = hasBody ? { "Content-Type":"application/json", ...(opts.headers||{}) } : (opts.headers||{});
  if (SESSION_TOKEN) headers["Authorization"] = "Bearer " + SESSION_TOKEN;
  const res = await fetch(`${BACKEND}${path}`, { ...opts, headers });
  if (!res.ok) {
    const t = await res.text().catch(()=> "");
    throw new Error(`HTTP ${res.status}: ${t || res.statusText}`);
  }
  return res.json();
}

// fetch con retry/timeout + logs
async function fetchJSON(url, { timeoutMs = 7000, retries = 1, requestId = Math.random().toString(16).slice(2), ...opts } = {}){
  let lastErr = null;
  for (let attempt = 0; attempt <= retries; attempt++){
    const ac = new AbortController();
    const to = setTimeout(()=> ac.abort(), timeoutMs);
    const t0 = performance.now();
    try{
      dlog(`→ [${requestId}] GET ${url} (attempt ${attempt+1}/${retries+1}, timeout ${timeoutMs}ms)`);
      const r = await fetch(url, { ...opts, signal: ac.signal, headers: { ...(opts.headers||{}), "x-request-id": requestId } });
      const t1 = performance.now();
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      const size = Array.isArray(j) ? j.length
                 : (Array.isArray(j.tokens) ? j.tokens.length : (j.count ?? "—"));
      dlog(`← [${requestId}] OK in ${Math.round(t1 - t0)}ms · size=${size}`);
      return j;
    }catch(e){
      const t1 = performance.now();
      lastErr = e;
      const aborted = (e?.name === "AbortError");
      dlog(`× [${requestId}] ${aborted ? "TIMEOUT" : "ERR"} in ${Math.round(t1 - t0)}ms:`, e?.message || e);
      if (attempt < retries){
        timeoutMs = Math.max(timeoutMs * 2.2, 16000);
        continue;
      }
      throw e;
    }finally{
      clearTimeout(to);
    }
  }
  throw lastErr || new Error("unknown fetch error");
}

/* ===================== UI helpers ===================== */
function setRunning(isOn){
  if (els.panelStatus) els.panelStatus.textContent = isOn ? "Running" : "Stopped";
}
function updateWalletUI(addr){
  const label = addr ? short(addr) : "Connect Wallet";
  if (els.walletLabel) els.walletLabel.textContent = label;
  if (els.mmIcon) els.mmIcon.classList.toggle("hidden", !addr);
  if (els.panelWallet) els.panelWallet.textContent = addr ? short(addr) : "0x…";
}
function openSidePanel(){
  if (!els.side || !els.sideBackdrop) return;
  refreshStatus().catch(()=>{});
  els.side.classList.remove("closing");
  els.side.classList.add("open");
  els.sideBackdrop.classList.add("visible");
}
function closeSidePanel(){
  if (!els.side || !els.sideBackdrop) return;
  els.side.classList.add("closing");
  els.side.classList.remove("open");
  els.sideBackdrop.classList.remove("visible");
  const onEnd = (ev)=>{
    if (ev.target !== els.side) return;
    els.side.removeEventListener("transitionend", onEnd);
    els.side.classList.remove("closing");
  };
  els.side.addEventListener("transitionend", onEnd, { once:true });
}

/* ===================== Status ===================== */
async function refreshStatus(){
  try{
    const s = await api("/bot/status");
    const isRunning = typeof s.running === "boolean" ? s.running
                   : (typeof s.state === "string" ? s.state.toLowerCase() === "running" : false);
    setRunning(!!isRunning);
    return !!isRunning;
  }catch{
    setRunning(false);
    return false;
  }
}

/* ===================== Wallet / Session ===================== */
async function ensureSessionAndAddr(){
  const cached = localStorage.getItem(LAST_ADDR_KEY) || "";
  if (cached) updateWalletUI(cached);
  if (SESSION_TOKEN && window.ethereum) {
    try{
      const accts = await window.ethereum.request({ method:"eth_accounts" });
      const addr = (accts && accts[0]) || cached;
      if (addr) {
        updateWalletUI(addr);
        return addr;
      }
    }catch{}
  }
  return cached;
}

async function connectWalletFlow(){
  if(!window.ethereum){ alert("No se detectó wallet. Instala MetaMask/Brave."); return ""; }
  const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
  const addr = (accounts && accounts[0]) || "";
  if(!addr) return "";

  localStorage.setItem(LAST_ADDR_KEY, addr);

  if (!SESSION_TOKEN) {
    const { nonce } = await api("/auth/nonce", { method:"POST", body: JSON.stringify({ address: addr }) });
    const msg = `OperatorLiquid login\nAddress: ${addr.toLowerCase()}\nNonce: ${nonce}`;
    const signature = await window.ethereum.request({ method: "personal_sign", params: [msg, addr] });
    const v = await api("/auth/verify", { method:"POST", body: JSON.stringify({ address: addr, signature }) });
    SESSION_TOKEN = v.token;
    localStorage.setItem(TOKEN_KEY, SESSION_TOKEN);
  }

  CONNECTED = true;
  updateWalletUI(addr);
  return addr;
}

function disconnectAppSession(){
  localStorage.removeItem(TOKEN_KEY);
  SESSION_TOKEN = "";
  CONNECTED = false;
  updateWalletUI("");
  setRunning(false);
}

async function stopBot(){
  try { await api("/bot/stop", { method:"POST" }); } catch {}
}

/* ===================== New (backend) ===================== */
async function refreshNew(){
  try{
    if (!els.newList) return;
    const r = await api("/liqd/new?limit=40");
    const data = r.data || r;
    els.newList.innerHTML = "";
    for(const it of data){ els.newList.appendChild(renderTokenRow(it)); }
  }catch(e){
    if (els.newList) els.newList.innerHTML = `<div class="sn-item">Error: ${e.message}</div>`;
  }
}
function renderTokenRow(item){
  const div = document.createElement("div");
  div.className = "sn-item";
  const left = document.createElement("div");
  left.className = "sn-left";
  const tok = document.createElement("div");
  tok.innerHTML = `<div class="sn-token">${item.metadata?.symbol || short(item.token)}</div>
                   <div class="sn-meta">${item.metadata?.name || short(item.token)} • block ${item.block}</div>`;
  left.appendChild(tok);
  const actions = document.createElement("div");
  actions.className = "sn-actions";
  const btn = document.createElement("button");
  btn.className = "btn snipe";
  btn.textContent = "Snipe";
  btn.onclick = ()=> doSnipe(item.token, btn);
  actions.appendChild(btn);
  div.appendChild(left);
  div.appendChild(actions);
  return div;
}
async function doSnipe(tokenAddr, btn){
  try{
    btn.disabled = true; btn.textContent = "Sniping…";
    const tokenIn = (els.tokenInInput?.value || "").trim();
    const amount = (els.amountInput?.value || "1").trim();
    const slippage = parseFloat(els.slippageInput?.value || "1.0");
    const body = { token: tokenAddr, tokenIn, amountIn: amount, slippage };
    const res = await api("/liqd/snipe", { method:"POST", body: JSON.stringify(body) });
    prependLog("[SNIPER] tx: " + (res.tx || JSON.stringify(res)));
    btn.textContent = "OK";
  }catch(e){
    prependLog("[SNIPER][ERROR] " + (e.message || e));
    btn.textContent = "Error";
  }finally{
    setTimeout(()=>{ btn.textContent = "Snipe"; btn.disabled = false; }, 1800);
  }
}
function prependLog(line){
  if (!els.newList) return;
  const p = document.createElement("div");
  p.style.fontFamily = "ui-monospace, monospace";
  p.style.fontSize = "13px";
  p.style.color = "#CFE8E2";
  p.style.marginTop = "6px";
  p.textContent = line;
  els.newList.prepend(p);
}

/* ===================== Cards ===================== */
function renderRecentCard(t, badge="LIQUIDLAUNCH"){
  const name = t.name || t.metadata?.name || (t.symbol ? t.symbol : "Unknown");
  const sym  = t.symbol || t.metadata?.symbol || "";
  const addr = (t.address || t.token || t.contract || "").toString();
  const rawTs = t.creationTimestamp || t.created_at || t.createdAt || null;
  let created = "—";
  if (rawTs) {
    const d = new Date(Number(rawTs) * 1000);
    if (!isNaN(d.getTime())) created = "Created • " + d.toLocaleString();
  }
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="t">${name}${sym?` <span class="pill">${sym}</span>`:""} <span class="pill">${badge}</span></div>
    <div class="s">${created}</div>
    <div class="addr">${addr ? addr.slice(0,6)+"…"+addr.slice(-4) : ""}</div>
    <div style="margin-top:6px;">
      ${addr? `<button class="btn tiny" type="button" data-copy="${addr}">Copy address</button>`:""}
    </div>
  `;
  const btn = el.querySelector('button[data-copy]');
  if (btn) btn.addEventListener('click', async ()=> {
    try{
      await navigator.clipboard.writeText(btn.getAttribute('data-copy'));
      const old = btn.textContent;
      btn.textContent = "Copied!";
      setTimeout(()=> btn.textContent = old, 1200);
    }catch{}
  });
  return el;
}

function renderRecentCardLaunch(t){
  const name = t.name || t.symbol || "Unknown";
  const sym  = t.symbol || "";
  const addr = (t.address || t.token || "").toString();
  const ts   = Number(t.creationTimestamp) || 0;
  const when = ts ? new Date(ts * 1000) : null;
  const badge = t.isBonded ? "BONDED" : "UNBONDED";

  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="t">${name}${sym?` <span class="pill">${sym}</span>`:""} <span class="pill">${badge}</span></div>
    <div class="s">${when ? "Created • " + when.toLocaleString() : "—"}</div>
    <div class="addr">${addr ? addr.slice(0,6)+"…"+addr.slice(-4) : ""}</div>
    <div style="margin-top:6px;">
      ${addr? `<button class="btn tiny" type="button" data-copy="${addr}">Copy address</button>`:""}
    </div>
  `;
  const btn = el.querySelector('button[data-copy]');
  if (btn) btn.addEventListener('click', async ()=>{
    try{
      await navigator.clipboard.writeText(addr);
      const old = btn.textContent; btn.textContent = "Copied!"; setTimeout(()=> btn.textContent = old, 1200);
    }catch{}
  });
  return el;
}

/* ===================== Recent (solo LiquidLaunch) ===================== */
async function loadRecent(){
  if (!els.recentSkeletons || !els.recentList) return;

  els.recentSkeletons.style.display = "grid";
  els.recentList.style.display = "none";
  els.recentList.innerHTML = "";

  let data = [], errorMsg = "";

  try{
    // 1) Solo tokens del contrato LiquidLaunch (BONDED + UNBONDED)
    const j = await fetchJSON(`${BACKEND}/liqd/recent_launch?limit=30&page_size=100`, { timeoutMs: 9000, retries: 1, requestId: "recent-launch" });
    data = Array.isArray(j.tokens) ? j.tokens : [];
    if (!data.length && j.error) errorMsg = String(j.error);

    // 2) Fallbacks opcionales
    if (!data.length) {
      const j2 = await fetchJSON(`${BACKEND}/liqd/recent_tokens_rpc?limit=30&bonded=both&page_size=100`, { timeoutMs: 9000, retries: 1, requestId: "recent-rpc" });
      const arr2 = Array.isArray(j2.tokens) ? j2.tokens : [];
      if (arr2.length) data = arr2;
      if (!data.length && j2.error && !errorMsg) errorMsg = String(j2.error);
    }
    if (!data.length) {
      const j3 = await fetchJSON(`${BACKEND}/liqd/recent_unbonded_chain?limit=30&page_size=100`, { timeoutMs: 9000, retries: 1, requestId: "recent-chain" });
      const arr3 = Array.isArray(j3.tokens) ? j3.tokens : [];
      if (arr3.length) data = arr3.map(t => ({ ...t, isBonded: false, bondedTimestamp: 0 }));
      if (!data.length && j3.error && !errorMsg) errorMsg = String(j3.error);
    }

  }catch(e){
    errorMsg = e?.message || String(e);
  } finally {
    try{
      if (data.length){
        data.sort((a,b)=> (b.creationTimestamp||0) - (a.creationTimestamp||0));
        for (const t of data) els.recentList.appendChild(renderRecentCardLaunch(t));
        try{ sessionStorage.setItem("liqd_recent_cache_launch", JSON.stringify(data.slice(0,30))); }catch{}
      } else {
        const cached = sessionStorage.getItem("liqd_recent_cache_launch");
        if (cached) {
          JSON.parse(cached).forEach(t => els.recentList.appendChild(renderRecentCardLaunch(t)));
        } else {
          const empty = document.createElement("div");
          empty.className = "card";
          empty.innerHTML = `<div class="t">Sin lanzamientos</div>
            <div class="s">${errorMsg ? "Backend: "+errorMsg : "No hay tokens de LiquidLaunch visibles ahora mismo."}</div>`;
          els.recentList.appendChild(empty);
        }
      }
    } finally {
      els.recentSkeletons.style.display = "none";
      els.recentList.style.display = "grid";
    }
  }
}

/* ===================== Frozen (opcional) ===================== */
async function loadFrozen(){
  if (els.frozenSkeletons && !els.frozenList) { els.frozenSkeletons.style.display = "none"; return; }
  if (!els.frozenSkeletons || !els.frozenList) return;

  els.frozenSkeletons.style.display = "grid";
  els.frozenList.style.display = "none";
  els.frozenList.innerHTML = "";

  let data = [], errorMsg = "";

  try{
    const j = await fetchJSON(`${BACKEND}/liqd/recent_frozen?limit=24`, { timeoutMs: 7000 });
    data = Array.isArray(j.tokens) ? j.tokens : [];
    if (!data.length && j.error) errorMsg = String(j.error);
  }catch(e){
    errorMsg = e?.message || String(e);
    dlog("[frozen] error:", errorMsg);
  } finally {
    try{
      if (data.length){
        data.sort((a,b)=> (b.frozenTimestamp||0) - (a.frozenTimestamp||0));
        for (const tkn of data) els.frozenList.appendChild(renderRecentCard(tkn, "FROZEN"));
        try{ sessionStorage.setItem("liqd_recent_cache_frozen", JSON.stringify(data)); }catch{}
      } else {
        const cached = sessionStorage.getItem("liqd_recent_cache_frozen");
        if (cached) {
          JSON.parse(cached).forEach(t => els.frozenList.appendChild(renderRecentCard(t, "FROZEN")));
        } else {
          const empty = document.createElement("div");
          empty.className = "card";
          empty.innerHTML = `<div class="t">Sin tokens frozen</div>
            <div class="s">${errorMsg ? "Backend: "+errorMsg : "No hay tokens listos para migrar."}</div>`;
          els.frozenList.appendChild(empty);
        }
      }
    } finally {
      els.frozenSkeletons.style.display = "none";
      els.frozenList.style.display = "grid";
    }
  }
}

/* ===================== Bindings ===================== */
(function bindSidePanel(){
  const bind = (node, handler)=>{
    if (!node) return;
    ["click","touchend","pointerup"].forEach(ev=>{
      node.addEventListener(ev, (e)=>{ e.preventDefault(); e.stopPropagation(); handler(); }, { passive:false });
    });
  };
  bind(els.sideClose, closeSidePanel);
  bind(els.sideBackdrop, closeSidePanel);

  if (els.panelDisconnect) {
    els.panelDisconnect.addEventListener("click", async (e)=>{
      e.preventDefault();
      try { await stopBot(); } catch {}
      disconnectAppSession();
      closeSidePanel();
    });
  }

  if (els.btnRefreshFrozen) {
    els.btnRefreshFrozen.addEventListener("click", (e)=>{
      e.preventDefault();
      loadFrozen().catch(()=>{});
    });
  }
})();
document.addEventListener("keydown", (e)=>{ if (e.key === "Escape") closeSidePanel(); });

els.walletBtn && (els.walletBtn.onclick = async ()=>{
  if (!CONNECTED) {
    const addr = await connectWalletFlow().catch(e=>{ alert(e.message || e); return ""; });
    if (addr) openSidePanel();
  } else {
    openSidePanel();
  }
});
els.btnRefresh && (els.btnRefresh.onclick = ()=> refreshNew());

if (window.ethereum && window.ethereum.on) {
  window.ethereum.on("disconnect", async () => {
    await stopBot();
    disconnectAppSession();
    closeSidePanel();
  });
  window.ethereum.on("accountsChanged", async (_accounts) => {
    await stopBot();
    disconnectAppSession();
    closeSidePanel();
    await ensureSessionAndAddr();
  });
  window.ethereum.on("chainChanged", async (_chainId) => {
    await stopBot();
    refreshStatus();
  });
}

/* ===================== init ===================== */
(async function init(){
  try{
    const health = await fetchJSON(`${BACKEND}/liqd/rpc_health`, { timeoutMs: 5000, requestId: "rpc-health" });
    window._liqd_debug = window._liqd_debug || {};
    window._liqd_debug.rpcHealth = health;
    dlog("rpc_health:", health);
    setDebugText(`<b>RPC Health</b><br>${(health?.rpcs||[]).map(r=>`${r.rpc} → ${r.connected?"OK":"FAIL"} ${r.blockNumber??""}`).join("<br>")}`);
  }catch(e){ dlog("rpc_health error:", e?.message||e); }

  if (els.backendUrl) els.backendUrl.textContent = BACKEND;
  if (SESSION_TOKEN) CONNECTED = true;
  await ensureSessionAndAddr();

  await refreshStatus();
  await refreshNew();
  await loadRecent();
  await loadFrozen();

  setInterval(()=> { refreshNew().catch(()=>{}); }, 4000);
  setInterval(()=> { loadRecent().catch(()=>{}); }, 12000);
  setInterval(()=> { loadFrozen().catch(()=>{}); }, 15000);
})();
