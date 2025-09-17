// PRODUCCIÓN por defecto → tu URL de Railway
const BACKEND = localStorage.getItem("BACKEND_BASE") || "https://hl-maker-webapp-production.up.railway.app";

// (nuevo) seteo defensivo y visibilidad del footer
const backendUrlEl = document.getElementById("backendUrl");
const footDebug = document.getElementById("footDebug");
const IS_LOCAL = ["localhost", "127.0.0.1"].includes(location.hostname);

// Mostrar footer solo en local o si forzás con flag
if (footDebug) {
  if (IS_LOCAL || localStorage.getItem("SHOW_BACKEND") === "1") {
    footDebug.classList.remove("hidden");
    if (backendUrlEl) backendUrlEl.textContent = BACKEND;
  } else {
    // en prod queda oculto (no hace falta ni rellenar el texto)
    footDebug.classList.add("hidden");
  }
}


const $ = (s)=>document.querySelector(s);
const els = {
  walletBtn: document.getElementById("btnConnect"),
  walletLabel: document.getElementById("walletLabel"),
  mmIcon: document.getElementById("mmIcon"),

  // main UI
  ticker: document.getElementById("ticker"),
  datalist: document.getElementById("tickers"),
  amount: document.getElementById("amount"),
  minSpread: document.getElementById("minSpread"),
  ttl: document.getElementById("ttl"),
  makerOnly: document.getElementById("makerOnly"),
  btnStart: document.getElementById("btnStart"),
  btnStop: document.getElementById("btnStop"),
  logBox: document.getElementById("logBox"),
  wsState: document.getElementById("wsState"),
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  btnConnectWS: document.getElementById("btnConnectWS"),
  btnDisconnectWS: document.getElementById("btnDisconnectWS"),

  // side panel
  side: document.getElementById("sidePanel"),
  sideBackdrop: document.getElementById("sideBackdrop"),
  sideClose: document.getElementById("sideClose"),
  panelWallet: document.getElementById("panelWallet"),
  panelStatus: document.getElementById("panelStatus"),
  panelDisconnect: document.getElementById("panelDisconnect"),
};

let ws=null, wsPing=null;
let ALL_TICKERS = [];
let SESSION_TOKEN = localStorage.getItem("hl_session_token") || "";
let AGENT_PK = localStorage.getItem("hl_agent_pk") || "";
let AGENT_ADDR = localStorage.getItem("hl_agent_addr") || "";
let AGENT_APPROVED = localStorage.getItem("hl_agent_approved") === "1";
let CONNECTED = !!SESSION_TOKEN;
let STOP_SENT = false;

// ===== UI helpers =====
function show(el){ el && el.classList.remove("hidden"); }
function hide(el){ el && el.classList.add("hidden"); }
function setRunning(isOn){
  if (els.statusDot) els.statusDot.classList.toggle("on", !!isOn);
  if (els.statusText) els.statusText.textContent = isOn ? "Running" : "Stopped";
  if (els.panelStatus) els.panelStatus.textContent = isOn ? "Running" : "Stopped";
}

const LOG_MAX_CHARS = 250000;  // ~250 KB de texto (ajustable)

function appendLog(line){
  const box = els.logBox;
  if(!box) return;
  const atBottom = Math.abs(box.scrollHeight - box.scrollTop - box.clientHeight) < 4;

  box.textContent += (box.textContent ? "\n" : "") + line;

  // recorte por tamaño (más rápido que contar líneas)
  if (box.textContent.length > LOG_MAX_CHARS) {
    box.textContent = box.textContent.slice(-LOG_MAX_CHARS);
  }

  if (atBottom) box.scrollTop = box.scrollHeight;
}
const short = (a)=> a ? (a.slice(0,6)+"…"+a.slice(-4)) : "";

// ===== API =====
async function api(path, opts={}){
  const hasBody = typeof opts.body !== "undefined";
  const headers = hasBody ? { "Content-Type":"application/json", ...(opts.headers||{}) } : (opts.headers||{});
  if (SESSION_TOKEN) headers["Authorization"] = "Bearer " + SESSION_TOKEN;
  const res = await fetch(`${BACKEND}${path}`, { ...opts, headers });
  if(!res.ok){
    const t = await res.text();
    throw new Error(`HTTP ${res.status}: ${t}`);
  }
  return res.json();
}
async function refreshStatus(){
  try{
    const s = await api("/bot/status");
    setRunning(!!s.running);
    return !!s.running;
  }catch{ return false; }
}

// ===== lazy imports =====
async function getEthers(){
  if (!window.__ethers) {
    window.__ethers = await import("https://esm.sh/ethers@6.13.2?bundle");
  }
  return window.__ethers;
}
async function getHL(){
  if (!window.__hl) {
    window.__hl = await import("https://esm.sh/@nktkas/hyperliquid@0.24.3/signing?bundle");
  }
  return window.__hl;
}

// ===== meta / tickers =====
function updateTickerSuggestions(query){
  const q = String(query||"").trim().toUpperCase();
  els.datalist.innerHTML = "";
  let shown = 0, MAX = 30;
  for(const sym of ALL_TICKERS){
    if(shown >= MAX) break;
    const base = sym.split("/")[0];
    if(q === "" || base.startsWith(q)){
      const opt = document.createElement("option");
      opt.value = sym;
      els.datalist.appendChild(opt);
      shown++;
    }
  }
}
async function loadMeta(){
  try{
    const data = await api("/meta/spot");
    const meta = Array.isArray(data.meta) ? data.meta : Object.values(data.meta||{});
    const set = new Set();
    for(const m of meta){
      const base = String(m.name||m.base||"").toUpperCase();
      const quote = String(m.quote||"USDC").toUpperCase();
      if(base) set.add(`${base}/${quote}`);
    }
    ALL_TICKERS = Array.from(set).sort();
    updateTickerSuggestions("");
  }catch(e){ appendLog("[UI][WARN] meta: "+e.message); }
}
els.ticker && els.ticker.addEventListener("input", (e)=> updateTickerSuggestions(e.target.value));

// ===== WS logs =====
function connectWS(){
  if(ws) return;
  if (els.wsState) els.wsState.textContent = "conectando…";
  const url = BACKEND.replace(/^http/i,"ws") + "/ws/logs" + (SESSION_TOKEN ? `?token=${encodeURIComponent(SESSION_TOKEN)}` : "");
  ws = new WebSocket(url);
  ws.onopen = ()=>{ if (els.wsState) els.wsState.textContent = "conectado"; wsPing = setInterval(()=>{ try{ ws.send("ping"); }catch{} }, 800); };
  ws.onmessage = (ev)=> appendLog(String(ev.data||""));
  ws.onerror = ()=> { if (els.wsState) els.wsState.textContent = "error"; };
  ws.onclose = ()=>{ if (els.wsState) els.wsState.textContent = "desconectado"; clearInterval(wsPing); wsPing=null; ws=null; };
}
function disconnectWS(){ if(ws){ try{ ws.close(); }catch{} } }
els.btnConnectWS && (els.btnConnectWS.onclick = connectWS);
els.btnDisconnectWS && (els.btnDisconnectWS.onclick = disconnectWS);

// ===== Agent local =====
async function ensureAgentLocal(){
  if (AGENT_PK && AGENT_ADDR) return { pk:AGENT_PK, addr:AGENT_ADDR };
  const { ethers } = await getEthers();
  const w = ethers.Wallet.createRandom();
  AGENT_PK = w.privateKey;
  AGENT_ADDR = w.address;
  localStorage.setItem("hl_agent_pk", AGENT_PK);
  localStorage.setItem("hl_agent_addr", AGENT_ADDR);
  appendLog("[UI] Agent local creado: " + short(AGENT_ADDR));
  return { pk:AGENT_PK, addr:AGENT_ADDR };
}

// ===== ApproveAgent =====
async function approveAgentIfNeeded(){
  if (AGENT_APPROVED) return true;
  const { ethers } = await getEthers();
  const { actionSorter, signUserSignedAction, userSignedActionEip712Types } = await getHL();

  const signatureChainId = await window.ethereum.request({ method: "eth_chainId" });
  const action = actionSorter.approveAgent({
    type: "approveAgent",
    hyperliquidChain: "Mainnet",
    signatureChainId,
    agentAddress: AGENT_ADDR,
    agentName: "OperatorLiquid",
    nonce: Date.now()
  });

  const provider = new ethers.BrowserProvider(window.ethereum);
  const signer = await provider.getSigner();

  const signature = await signUserSignedAction({
    wallet: signer,
    action,
    types: userSignedActionEip712Types[action.type],
  });

  const res = await fetch("https://api.hyperliquid.xyz/exchange", {
    method: "POST",
    headers: { "Content-Type":"application/json" },
    body: JSON.stringify({ action, signature, nonce: action.nonce }),
  });
  const body = await res.json().catch(()=> ({}));
  if (res.ok && body?.status === "ok") {
    AGENT_APPROVED = true;
    localStorage.setItem("hl_agent_approved","1");
    appendLog("[UI] Agent aprobado en Hyperliquid.");
    return true;
  } else {
    const msg = (body?.response || JSON.stringify(body||{})).toLowerCase();
    if (msg.includes("extra agent already used") || msg.includes("already used") || msg.includes("already")) {
      AGENT_APPROVED = true;
      localStorage.setItem("hl_agent_approved","1");
      appendLog("[UI] Agent ya estaba aprobado; continuamos.");
      return true;
    }
    appendLog("[UI][ERROR] approveAgent: " + JSON.stringify(body||{}));
    throw new Error("approveAgent falló");
  }
}

// ===== Wallet connect + login =====
async function connectWalletFlow(){
  if(!window.ethereum){ alert("No se detectó wallet. Instala MetaMask/Brave."); return; }
  const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
  const addr = (accounts && accounts[0]) || "";
  if(!addr) return;

  // login con firma al backend (sesión/token) SOLO si no teníamos
  if (!SESSION_TOKEN) {
    const { nonce } = await api("/auth/nonce", { method:"POST", body: JSON.stringify({ address: addr }) });
    const msg = `OperatorLiquid login\nAddress: ${addr.toLowerCase()}\nNonce: ${nonce}`;
    const signature = await window.ethereum.request({ method: "personal_sign", params: [msg, addr] });
    const v = await api("/auth/verify", { method:"POST", body: JSON.stringify({ address: addr, signature }) });
    SESSION_TOKEN = v.token;
    localStorage.setItem("hl_session_token", SESSION_TOKEN);
  }

  await ensureAgentLocal();
  await approveAgentIfNeeded();

  CONNECTED = true;
  updateWalletUI(addr);
}

function updateWalletUI(addr){
  if (els.walletLabel) els.walletLabel.textContent = short(addr);
  if (els.mmIcon) show(els.mmIcon);
}

// ===== Stop helpers =====
async function stopBotSoft() { try { await api("/bot/stop", { method: "POST" }); } catch {} }
function stopBotBeacon() {
  try {
    if (STOP_SENT) return;
    STOP_SENT = true;
    const token = localStorage.getItem("hl_session_token") || "";
    if (!token) return;
    const data = "token=" + encodeURIComponent(token);
    const blob = new Blob([data], { type: "text/plain;charset=UTF-8" });
    navigator.sendBeacon(`${BACKEND}/bot/stop_by_token`, blob);
  } catch {}
}
async function stopBot() {
  await stopBotSoft();
  stopBotBeacon();
  disconnectWS();
  await refreshStatus();
}
function disconnectAppSession() {
  // NO tocamos agent ni su flag para evitar re-aprobar al reconectar
  localStorage.removeItem("hl_session_token");
  SESSION_TOKEN = "";
  CONNECTED = false;
  STOP_SENT = false;
  if (els.walletLabel) els.walletLabel.textContent = "Connect Wallet";
  if (els.mmIcon) hide(els.mmIcon);
  setRunning(false);
}

// ===== Side panel =====
function openSidePanel(){
  if (!els.side || !els.sideBackdrop) return;
  if (els.panelWallet && els.walletLabel) els.panelWallet.textContent = els.walletLabel.textContent || "";
  refreshStatus();
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

// 🔌 Enlazar la X, el backdrop y el botón Disconnect para cerrar
function bindSidePanelEvents(){
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
      appendLog("[UI] Wallet disconnected.");
    });
  }
}
bindSidePanelEvents();

document.addEventListener("keydown", (e)=>{
  if (e.key === "Escape") closeSidePanel();
});

// ===== Start / Stop =====
function disableActions(b){ if(els.btnStart) els.btnStart.disabled=b; if(els.btnStop) els.btnStop.disabled=b; }

async function handleWalletButtonClick(){
  if (!CONNECTED) {
    try {
      await connectWalletFlow();
    } catch(e) { appendLog("[UI][ERROR] connectWallet: " + (e?.message||e)); }
  } else {
    openSidePanel();
  }
}
els.walletBtn && (els.walletBtn.onclick = handleWalletButtonClick);

els.btnStart && (els.btnStart.onclick = async ()=>{
  try{
    disableActions(true);
    if(els.logBox) els.logBox.textContent = "";

    if(!SESSION_TOKEN || !CONNECTED){ await connectWalletFlow(); }

    const t = (els.ticker.value || "UBTC/USDC").toUpperCase();
    const body = {
      ticker: t,
      amount_per_level: parseFloat(els.amount.value || "12"),
      min_spread: parseFloat(els.minSpread.value || "0.006"),
      ttl: parseFloat(els.ttl.value || "20"),
      maker_only: !!els.makerOnly.checked,
      testnet: false,
      use_agent: true,
      agent_private_key: AGENT_PK
    };
    const r = await api("/bot/start",{ method:"POST", body: JSON.stringify(body) });
    appendLog("[UI] start: " + JSON.stringify(r));
    connectWS();
    await refreshStatus();
  }catch(e){ appendLog("[UI][ERROR] " + e.message); }
  finally{ disableActions(false); }
});

els.btnStop && (els.btnStop.onclick = async ()=>{
  try{
    disableActions(true);
    await stopBot();
    setTimeout(refreshStatus, 600);
  }catch(e){ appendLog("[UI][ERROR] " + e.message); }
  finally{ disableActions(false); }
});

// ===== Eventos de wallet =====
if (window.ethereum && window.ethereum.on) {
  window.ethereum.on("disconnect", async () => {
    await stopBot();
    disconnectAppSession();
  });
  window.ethereum.on("accountsChanged", async (_accounts) => {
    await stopBot();
    disconnectAppSession();
  });
  window.ethereum.on("chainChanged", async (_chainId) => {
    await stopBot();
  });
}

/*
IMPORTANTE:
Antes teníamos esto que cortaba la sesión al cerrar/recargar:
window.addEventListener("pagehide", ...disconnectAppSession());
window.addEventListener("beforeunload", ...disconnectAppSession());
AHORA lo removimos para que en "refresh" se mantenga el token y puedas parar el bot.
Si querés volver al comportamiento de “matar al cerrar”, avisame y te doy el flag opcional.
*/

// ===== init =====
(async function(){
  if(SESSION_TOKEN){
    CONNECTED = true;
    if (els.walletLabel) els.walletLabel.textContent = "Connected";
    if (els.mmIcon) show(els.mmIcon);
    connectWS(); // reconectar logs automáticamente después de un refresh
  }
  await refreshStatus();
  await loadMeta();
})();
