/* Listen window — vanilla JS over the pywebview bridge. Polls state every 250 ms (33 ms while recording, 1 s while the window is hidden); delivery status at most once a second and never while recording. */
(() => {
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const OPTS = { hotkey: { "Right Ctrl": "ctrl_r", "Right Shift": "shift_r", "Right Alt": "alt_r", "Pause": "pause", "Scroll Lock": "scroll_lock" }, style: { "Tighten rambles": "tighten", "Clean only": "clean" }, target: { "Claude Code": "claude_code", "General AI": "general" }, judge: { "Claude (API)": "anthropic", "Local model": "local" }, judge_model: { "Haiku 5.5 · budget": "claude-haiku-5-5", "Sonnet 5.5 · deeper critique": "claude-sonnet-5-5" }, engine: { "Claude (API)": "anthropic", "Local model": "local" } };

// ---------- bridge (pywebview) with a browser-only mock for design work ----------
const mock = {
  _s: { state: "idle", busy: "", lab_ver: 1, lab_busy: "", history_len: 3, llm_ok: true, last_error: "", locked: false, event: "" },
  _lab: { revs: [{ i: 0, kind: "typed", score: 6, stale: false, by: null }, { i: 1, kind: "improve", score: 8, stale: false, by: "claude-opus-5" }], cur: 1,
    text: "Goal: Add a dark mode toggle to the settings page of my Expo app.\n\n<context>\nThe settings page is app/settings.tsx. No new packages.\n</context>\n\n<task>\nImplement the toggle; persist the choice; default to the system setting.\n</task>\n\n<done>\nFlip it, reload, the choice survives.\n</done>",
    by: "claude-opus-5", stale: false, prev_score: 6, fav: null, busy: "", error: "", session_cost: 0.066,
    score: { score: 8, criteria: { clarity: 5, context: 4, task: 5, constraints: 4, success_criteria: 3, format: 5 }, gaps: ["No verification command.", "Persistence API unnamed while new packages are forbidden.", "Theming setup unknown."], improved: "Goal: …improved…", by: "claude-opus-5", cost: 0.033, note: "" } },
  get_state: async () => ({ ...mock._s, recording_ready: mock._s.state === "recording", lab_busy: mock._lab.busy, levels: mock._s.state === "recording" ? Array.from({ length: 20 }, () => Math.random() * 0.1) : [] }),
  get_options: async () => {
    const models={"Parakeet Unified English 0.6B (default)":"parakeet-unified-en-0.6b","Whisper Small":"small","Whisper Medium":"medium","Whisper Large v3":"large-v3","Whisper Large v3 Turbo":"large-v3-turbo"};
    const selected=mock._settings?.speech_model||"parakeet-unified-en-0.6b";
    return {...OPTS,speech_models:models,speech_model:selected,speech_model_label:Object.entries(models).find(([,value])=>value===selected)?.[0]||selected,speech_model_note:"Simulated model choices. No installed cache is checked in this demo."};
  },
  get_history: async () => [{ ts: "2026-09-12T10:12:00", kind: "dictate", text: "Add a dark mode toggle to the settings page and remember the choice between launches.", raw: "um add a dark mode toggle", secs: 6.2, latency: 0.9, cost: 0 }, { ts: "2026-09-11T18:02:00", kind: "dictate", text: "Ship it before Friday and let QA know.", raw: "ship it uh before friday", secs: 3.1, latency: 0.7, cost: 0 }],
  get_stats: async () => ({ today: 7, week: 41, words_week: 3120, minutes_week: 22.4, wpm: 139, latency: 0.8, spend_month: 0.42 }),
  get_lab: async () => mock._lab,
  _op: (busy, ms, then) => { mock._lab.busy = busy; mock._s.lab_ver++; setTimeout(() => { mock._lab.busy = ""; then && then(); mock._s.lab_ver++; }, ms); return true; },
  score: async () => mock._op("scoring", 800),
  select_rev: async i => { mock._lab.cur = i; mock._s.lab_ver++; return true; },
  copy_text: async () => true, paste_draft: async () => true, clear_history: async () => { mock._s.history_len = 0; return true; },
  diagnostics: async () => ({ speech: "faster-whisper small on CUDA", "clean-up": "qwen2.5:7b via Ollama at 127.0.0.1", "prompt lab": "engineer claude-opus-5, judge claude-opus-5, ANTHROPIC_API_KEY found", files: "…" }),
  get_vocab_suggestions: async () => ["Expo", "Suno", "pywebview"], add_vocab: async () => true, dismiss_vocab: async () => true, lab_kept: async () => true,
  get_learned: async () => ({ lines: ["Kept prompts run about 140 words and use XML-style section tags (7 kept).", "Weakest rubric criterion so far: success criteria (3.2/5)."], snippet: "…", kept: 7, enabled: true }), reset_learned: async () => true,
};
// Synthetic browser demo implements the same acknowledgement contract without side effects.
Object.assign(mock._lab,{draft_id:"demo-1",workspace_id:"demo-1",composer:"",pending:null,saved:true,save_error:""});
mock._archives=[];mock._favorites=[{name:"Demo review",text:"Review this synthetic example",uses:1}];mock._undo={};mock._settings={hotkey:"ctrl_r",cleanup:true,style:"tighten",vocab:"ExampleName",target:"general",judge:"local",engine:"local",local_model:"qwen2.5:7b",cloud_enabled:false,learn:true};
mock._settings.judge_model="claude-haiku-5-5";
mock._settings.live_preview=true;
mock.get_settings=async()=>({...mock._settings});mock.set_setting=async(k,v)=>{mock._settings[k]=v;return true;};
mock.set_text=async(text,id)=>{if(id!==mock._lab.draft_id)return false;mock._lab.text=text;if(mock._lab.pending)mock._lab.pending.stale=true;mock._s.lab_ver++;return true;};
mock.set_composer=async(text,id)=>{if(id!==mock._lab.draft_id)return false;mock._lab.composer=text;return true;};
mock.get_local_models=async()=>({models:["qwen2.5:7b"],selected:mock._settings.local_model,error:""});
mock.list_drafts=async()=>mock._archives.map(r=>({id:r.draft_id,title:r.text.slice(0,50)||"Empty draft"}));
mock.new_draft=async()=>{mock._archives.push(structuredClone(mock._lab));Object.assign(mock._lab,{draft_id:"demo-"+Date.now(),workspace_id:"demo-"+Date.now(),text:"",composer:"",pending:null,revs:[],cur:-1,score:null});mock._s.lab_ver++;return true;};
mock.restore_draft=async id=>{const i=mock._archives.findIndex(r=>r.draft_id===id);if(i<0)return false;const next=mock._archives.splice(i,1)[0];mock._archives.push(structuredClone(mock._lab));mock._lab=next;mock._s.lab_ver++;return true;};
const demoCandidate=async(kind,text)=>{
  if(mock._lab.pending || mock._lab.busy)return false;
  return mock._op("generating",600,()=>{if(!mock._lab.pending)mock._lab.pending={id:"candidate-"+Date.now(),text:text||mock._lab.text+"\n\nVerify the result with a focused check.",kind,by:"Synthetic demo model",source_text:mock._lab.text,stale:false};});
};
mock.improve=()=>demoCandidate("improve");mock.rebuild=()=>demoCandidate("rebuild");mock.submit=text=>demoCandidate("refine",mock._lab.text+"\n\nInstruction: "+text);
mock.apply_candidate=async id=>{const p=mock._lab.pending;if(!p||p.id!==id||p.stale||p.source_text!==mock._lab.text)return false;mock._lab.text=p.text;mock._lab.revs.push({i:mock._lab.revs.length,kind:p.kind,by:p.by,score:null});mock._lab.cur=mock._lab.revs.length-1;mock._lab.by=p.by;mock._lab.score=null;mock._lab.pending=null;mock._s.lab_ver++;return true;};
mock.discard_candidate=async id=>{if(mock._lab.pending?.id!==id)return false;mock._lab.pending=null;mock._s.lab_ver++;return true;};
mock.get_favorites=async()=>mock._favorites;mock.save_favorite_checked=async(name,text,replace=false)=>{const old=mock._favorites.find(f=>f.name===name);if(old&&!replace)return {status:"conflict"};if(old)old.text=text;else mock._favorites.push({name,text,uses:0});return {status:"saved"};};
mock.remove_favorite=async name=>{const i=mock._favorites.findIndex(f=>f.name===name);if(i<0)return null;const token="undo-"+Date.now();mock._undo[token]=mock._favorites.splice(i,1)[0];return token;};
mock.undo_favorite_removal=async token=>{const f=mock._undo[token];if(!f||mock._favorites.some(x=>x.name===f.name))return false;mock._favorites.push(f);delete mock._undo[token];return true;};
mock.load_favorite=async name=>{const f=mock._favorites.find(x=>x.name===name);if(!f)return false;await mock.new_draft();mock._lab.text=f.text;mock._s.lab_ver++;return true;};
mock.get_retained=async()=>[];mock.get_microphones=async()=>({"Demo microphone (simulated)":null});
mock.delete_draft=async(id,confirmation)=>{if(confirmation!=="DELETE")return false;const i=mock._archives.findIndex(x=>String(x.draft_id)===String(id));if(i<0)return false;mock._archives.splice(i,1);return true;};
mock.finish_recording=async()=>{mock._s.state="idle";return true;};mock.cancel_recording=mock.finish_recording;mock.retry_startup=mock.finish_recording;
mock.clear_private_data=async(scope,confirmation)=>{if(confirmation!=="DELETE")return {ok:false,message:"Canceled"};if(scope==="workspace"){mock._archives=[];await mock.new_draft();mock._archives=[];}if(scope==="history")mock.get_history=async()=>[];return {ok:true,message:"Demo "+scope+" cleared (synthetic data only)"};};
const demo = new URLSearchParams(location.search).get("demo") === "1";
let bridgeReady = !!window.pywebview?.api, bridgeLost = false, booted = false;
const api = () => { if (demo) return mock; if (!bridgeReady || !window.pywebview?.api) throw new Error("Desktop connection unavailable"); return window.pywebview.api; };
function connection(message) { const box = $("#bridge-status"); box.classList.toggle("hidden", !message); if (box.textContent !== message) box.textContent = message; }
function recovery(message) {
  const el = $("#recovery-status");
  if (el.textContent !== message) {
    el.textContent = message;
    el.dataset.saved = String(message === "Saved locally");
  }
}
const htmlCache = new WeakMap();
function stableHTML(el, html) { if (htmlCache.get(el) !== html) { el.innerHTML = html; htmlCache.set(el, html); } }


// ---------- state ----------
let S = {}, settings = {}, options = {}, lab = { revs: [], text: "" }, favs = [];
let lastLabVer = -1, lastHistLen = -1, page = "dictate", editing = false, lastBusy = "";

// ---------- live preview ----------
let previewUiEpoch = 0, previewLastVersion = -1, previewActionPending = 0, previewSettingVersion = 0;
function clearPreviewDom() {
  $("#live-preview").classList.add("hidden");
  for(const id of ["#live-preview-text","#live-preview-phase"]) {
    const element=$(id);if(element.textContent)element.textContent="";
  }
}
function invalidatePreview(version) {
  previewUiEpoch++;
  if(Number.isInteger(version))previewLastVersion=Math.max(previewLastVersion,version);
  clearPreviewDom();
}
function renderPreview(st, epoch) {
  if(epoch!==previewUiEpoch)return;
  if(settings.live_preview===false||previewActionPending) {clearPreviewDom();return;}
  const preview=st.preview||{};
  if(!Number.isInteger(preview.version)) {clearPreviewDom();return;}
  if(preview.version<previewLastVersion)return;
  previewLastVersion=preview.version;
  if(preview.phase==="final"&&Number.isFinite(preview.dismiss_at)&&Number.isFinite(st.preview_now)&&st.preview_now>=preview.dismiss_at){clearPreviewDom();return;}
  if(!preview.active&&!["finishing","final"].includes(preview.phase)) {clearPreviewDom();return;}
  const text=typeof preview.text==="string"?preview.text:"";
  const phase=preview.phase==="finishing" ? "Finishing the full recording…"
    : preview.phase==="final" ? preview.final_outcome==="review" ? "Needs review in Listen. Nothing sent." : preview.final_outcome==="failed" ? "Final processing failed. Check Listen." : "Final recognized transcript."
    : preview.state==="paused" ? preview.reason==="slow" ? "Preview paused to keep final dictation responsive." : "Preview is unavailable. Recording controls remain available."
    : preview.state==="updating" ? "Updating preview…" : "Listening…";
  $("#live-preview").classList.remove("hidden");
  const heading=preview.phase==="final"?"Final recognized transcript":"Recording transcript · provisional";
  const note=preview.phase==="final"?"The actual result and delivery status are shown in Listen.":"Provisional. Final text is generated when you finish.";
  if($("#live-preview-title").textContent!==heading)$("#live-preview-title").textContent=heading;
  if($("#live-preview-note").textContent!==note)$("#live-preview-note").textContent=note;
  if($("#live-preview-text").textContent!==text)$("#live-preview-text").textContent=text;
  if($("#live-preview-phase").textContent!==phase)$("#live-preview-phase").textContent=phase;
}

// ---------- toasts ----------
const TOAST_MS = { info: 2400, err: 8000 };
function toast(msg, err = false) {
  const t = document.createElement("div"); t.className = "toast" + (err ? " err" : ""); t.setAttribute("role", err ? "alert" : "status");
  const m = document.createElement("span"); m.textContent = msg; t.appendChild(m);
  let timer;
  const close = () => { clearTimeout(timer); t.classList.add("out"); setTimeout(() => t.remove(), 300); };
  const arm = () => { clearTimeout(timer); timer = setTimeout(close, err ? TOAST_MS.err : TOAST_MS.info); };
  if (err) { const x = document.createElement("button"); x.className = "icon-btn"; x.setAttribute("aria-label", "Dismiss"); x.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-x"/></svg>'; x.onclick = close; t.appendChild(x); }
  t.onmouseenter = t.onfocusin = () => clearTimeout(timer); t.onmouseleave = t.onfocusout = arm;
  $("#toasts").appendChild(t); arm();
}
// Replaces window.confirm(): the same labelled <dialog> pattern as favorite naming. Resolves true only on Continue.
function askConfirm(message) {
  const dialog = $("#confirm-dialog"), opener = document.activeElement;
  if (dialog.open) return Promise.resolve(false);
  $("#confirm-message").textContent = message; dialog.returnValue = "";
  return new Promise(resolve => { dialog.addEventListener("close", () => { opener?.focus(); resolve(dialog.returnValue === "ok"); }, { once: true }); dialog.showModal(); });
}

// ---------- pages ----------
function show(p, focus = true) {
  for (const panel of $$(".lab-popover:popover-open")) panel.hidePopover();
  if (page !== p) $(".main").scrollTop = 0;
  page = p;
  $$(".page").forEach(el => el.classList.toggle("active", el.dataset.page === p));
  $$(".rail-btn").forEach(b => { const on = b.dataset.page === p; b.classList.toggle("active", on); if (on) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current"); });
  closeMore(false);
  if (p === "history") loadHistory();
  if (p === "settings") renderSettings();
  if (focus) (p === "lab" ? (lab.text ? $("#composer") : $("#draft")) : $(`.page[data-page="${p}"] h1`))?.focus();
}
$$(".rail-btn").forEach(b => b.onclick = () => show(b.dataset.page));
$$("[data-goto]").forEach(b => b.onclick = () => show(b.dataset.goto));
$(".main").addEventListener("focusin", event => {
  if (!event.target.matches(".frame-input, .editor")) return;
  const frame = event.target.closest("fieldset.frame"), workspace = $(".main");
  if (!frame) return;
  const viewport = workspace.getBoundingClientRect(), bounds = frame.getBoundingClientRect();
  const top = bounds.top - viewport.top, bottom = bounds.bottom - viewport.top;
  if (bounds.height > workspace.clientHeight - 36 || top < 18) workspace.scrollTop += top - 18;
  else if (bottom > workspace.clientHeight - 18) workspace.scrollTop += bottom - workspace.clientHeight + 18;
});

// ---------- bars ----------
function mkBars(el, n = 20) { el.innerHTML = Array.from({ length: n }, () => "<i></i>").join(""); }
mkBars($("#bars-dictate")); mkBars($("#bars-lab"));
const renderedBarHeights = new WeakMap();
function drawBars(levels) {
  const lv = [...Array(Math.max(0, 20 - levels.length)).fill(0), ...levels.slice(-20)];
  for (const el of [$("#bars-dictate"), $("#bars-lab")])
    $$("i", el).forEach((b, i) => {
      const height = (4 + Math.min(1, lv[i] * 6) * 22) + "px", previous = renderedBarHeights.get(b);
      if (previous?.requested === height && previous.applied === b.style.height) return;
      b.style.height = height;
      renderedBarHeights.set(b, {requested: height, applied: b.style.height});
    });
}

// ---------- chips ----------
function chip(key, label, icon) {
  const opts = options[key] || {};
  return `<label class="chip"><svg class="ico" aria-hidden="true"><use href="#${icon}"/></svg><span class="k">${label}</span><select data-key="${key}">${Object.entries(opts).map(([n, v]) => `<option value="${esc(v)}"${settings[key] === v ? " selected" : ""}>${esc(n)}</option>`).join("")}</select></label>`;
}
function renderChips() {
  const a = document.activeElement, box = a?.closest?.(".chips")?.id;
  const sel = a?.dataset?.key ? `[data-key="${a.dataset.key}"]` : a?.dataset?.toggle ? `[data-toggle="${a.dataset.toggle}"]` : "";
  stableHTML($("#chips-dictate"), chip("style", "Style", "i-sparkle") +
    `<button class="chip toggle ${settings.cleanup ? "" : "off"}" data-toggle="cleanup"><svg class="ico" aria-hidden="true"><use href="#i-sparkle"/></svg>AI clean-up ${settings.cleanup ? "on" : "off"}</button>`);
  const cloudSelected = settings.engine === "anthropic" || settings.judge === "anthropic";
  const cloudActive = cloudSelected && settings.cloud_enabled;
  const processing = cloudActive ? "Cloud API processing · paid calls"
    : cloudSelected ? "Local processing · cloud use is disabled" : "Local processing · stays on this computer";
  stableHTML($("#chips-lab"), `<span class="processing-label">${processing}</span>` + chip("target", "Target", "i-clipboard") + chip("engine", "Engineer", "i-sparkle") + chip("judge", "Judge with", "i-chart") + (settings.judge === "anthropic" ? chip("judge_model", "Cloud judge", "i-chart") : ""));
  $$(".chip select").forEach(s => s.onchange = () => setSetting(s.dataset.key, s.value));
  $$(".chip[data-toggle]").forEach(b => b.onclick = () => setSetting(b.dataset.toggle, !settings[b.dataset.toggle]));
  const name = k => Object.entries(options[k] || {}).find(([, v]) => v === settings[k])?.[0] || settings[k];
  const hotkeyName = String(name("hotkey") || "Right Ctrl");
  $("#hero-key").textContent = hotkeyName.replace(/^Right /, "");
  $("#hero-key-side").textContent = hotkeyName.startsWith("Right ") ? "right key" : "hold to speak";
  $("#composer-key").textContent = name("hotkey");
  $("#lab-model-label").textContent = cloudActive ? "Cloud API" : cloudSelected ? "Cloud disabled" : "Local model";
  $("#rail-processing").textContent = cloudActive ? "Cloud API enabled" : "Local processing";
  if (box && sel) $(`#${box} ${sel}`)?.focus(); // the chip row is rebuilt when a value changes; keep keyboard focus on the same control
}
async function setSetting(k, v) {
  if (k === "cloud_enabled" && v && !await askConfirm("Enable cloud processing? Prompt Lab and voice-triggered prompt engineering text, instructions and relevant preferences will be sent to the configured API provider. Improve can make multiple paid calls. Estimates are not your bill.")) return;
  const value = k === "microphone" && v === "" ? null : v;
  const speechChange = k === "speech_model", speechControl = speechChange ? $("#speech-model") : null;
  if(speechChange) {
    if(speechModelSavePending)return;
    speechModelSavePending=true; speechOptionsVersion++;
  }
  if (speechControl) {
    speechControl.dataset.saving = "true"; speechControl.disabled = true;
    $("#speech-model-status").textContent = "Changing speech model…";
  }
  const previewChange=k==="live_preview";
  if(previewChange) {previewSettingVersion++;previewActionPending++;settings.live_preview=!!value;invalidatePreview();}
  const settingPreviewVersion=previewSettingVersion;
  if(k === "local_model") modelChoiceUnconfirmed = true;
  try {
    try {
      const saved = await api().set_setting(k, value);
      if (!saved) toast(speechChange ? "Speech model change did not complete. Check the selected model and speech status; finish any current recording or task before trying again." : k === "local_model" ? "Model choice could not be saved. Refresh the list and choose an available Ollama model." : "Setting could not be changed. Stop recording or reconnect the selected microphone.", true);
      if(k === "local_model") { modelChoiceUnconfirmed = false; if(saved) settings.local_model = value; }
    } catch (e) { toast(speechChange ? "Speech model change was not confirmed. Check the selected model and speech status before continuing." : k === "local_model" ? "Model choice was not confirmed. Refresh the list to check your saved choice." : previewChange?"Preview setting was not confirmed. Check Settings before continuing.":"Setting could not be saved. Your previous choice is preserved.", true); }
    const latest=await api().get_settings();
    if (speechChange && api().get_options) options = await api().get_options();
    if(k === "local_model") modelChoiceUnconfirmed = false;
    if(previewChange&&settingPreviewVersion!==previewSettingVersion)return;
    settings=settingPreviewVersion===previewSettingVersion?latest:{...latest,live_preview:settings.live_preview};
    renderChips();
    if (page === "settings") syncSettings();
  } catch {
    toast("Setting status could not be refreshed. Reopen Settings to check your choice.",true);
  } finally {
    if(previewChange) {previewActionPending--;invalidatePreview();}
    if(speechChange) {speechModelSavePending=false; speechOptionsVersion++;}
    if(speechControl) { delete speechControl.dataset.saving; speechControl.disabled = speechControl.dataset.available === "false"; $("#speech-model-status").textContent = ""; }
  }
}

// ---------- settings page ----------
const SETTING_ROWS = [
  ["hotkey", "Hotkey", "Hold to dictate, release to paste. In the Prompt lab it dictates into the focused box."],
  ["speech_model", "Speech recognition model", "Choose a model installed on this computer. Finish or cancel a recording before changing it.", "speech"],
  ["microphone", "Microphone", "Reconnect your saved microphone or explicitly choose the Windows default. Changes apply to the next recording."],
  ["live_preview", "Live transcript preview", "Show raw, provisional speech locally while recording. Updates may pause when processing is slow; final text is generated when you finish.", "toggle"],
  ["cleanup", "AI clean-up", "Fix grammar and fillers with the local model", "toggle"], ["style", "Clean-up style", "How much the wording is tightened"],
  ["local_model", "AI text model (Ollama)", "Shared by text cleanup and local Prompt Lab writing and scoring. Speech recognition uses the separate model above.", "model"],
  ["target", "Prompt target", "Shapes how prompts are structured"], ["engine", "Engineer with", "Writes and refines prompts with Claude via API, or your selected local model"],
  ["judge", "Judge with", "Scores revisions against current guidance and writes the improved version"],
  ["judge_model", "Cloud judge model", "Haiku is the budget default; Sonnet offers deeper critique. Applies when Claude and cloud processing are enabled."],
  ["cloud_enabled", "Allow cloud processing", "Sends Prompt Lab and voice-triggered prompt engineering text and relevant preferences to the selected API provider. Ordinary dictation cleanup stays local. Multiple paid calls may be made.", "toggle"],
];
let speechModelSavePending=false, speechOptionsVersion=0;
const swHTML = on => `<span class="sw-t" aria-hidden="true">${on ? "On" : "Off"}</span>`;
function setSwitch(b, on) {
  b.classList.toggle("on", on); b.setAttribute("aria-checked", String(on));
  const t = b.querySelector(".sw-t"); if (t) t.textContent = on ? "On" : "Off";
}
// Built once when the page opens. Later changes go through syncSettings so controls (and keyboard focus) are kept.
function renderSettings() {
  const rows = new Map(SETTING_ROWS.map(row => [row[0], row]));
  const speechChoices = () => {
    const entries = Object.entries(options.speech_models || {[options.speech_model_label || options.speech_model || "Parakeet (default)"]: options.speech_model || "parakeet-unified-en-0.6b"});
    const selected = settings.speech_model || options.speech_model || "parakeet-unified-en-0.6b";
    return {available:entries.length>0, html: (!entries.some(([,value])=>value===selected) ? `<option value="${esc(selected)}" selected disabled>${esc(options.speech_model_label || selected)} (saved; unavailable)</option>` : "") + entries.map(([name,value])=>`<option value="${esc(value)}"${value===selected?" selected":""}>${esc(name)}${value==="parakeet-unified-en-0.6b"&&!/default/i.test(name)?" (default)":""}</option>`).join("")};
  };
  const control = key => {
    const [k, l, , t] = rows.get(key);
    return t === "toggle" ? `<button class="switch ${settings[k] ? "on" : ""}" data-toggle="${k}" role="switch" aria-checked="${!!settings[k]}" aria-label="${l}">${swHTML(!!settings[k])}</button>`
      : t === "speech" ? `<select id="speech-model" data-key="speech_model" data-available="${speechChoices().available}" aria-label="${l}" aria-describedby="speech-stage-summary speech-model-note speech-model-status"${speechChoices().available&&!speechModelSavePending?"":" disabled"}>${speechChoices().html}</select>`
      : t === "model" ? `<select id="local-model" aria-label="${l}" aria-describedby="model-status"></select><button class="btn quiet" id="refresh-models">Refresh</button>`
      : `<select data-key="${k}" aria-label="${l}">${Object.entries(options[k] || {}).map(([n, v]) => `<option value="${esc(v)}"${settings[k] === v ? " selected" : ""}>${esc(n)}</option>`).join("")}</select>`;
  };
  const row = k => `<div class="setting"><div><div class="l">${rows.get(k)[1]}</div><div class="d">${rows.get(k)[2]}</div></div><div class="setting-controls">${control(k)}</div></div>`;
  $("#settings-grid").innerHTML = `<p class="settings-intro">Speech captures your words. AI cleanup gives them readable structure. Prompt Lab writing and review are separate steps.</p>
    <div class="model-stages">
      <section class="stage-row" aria-labelledby="stage-speech"><div><h2 id="stage-speech">Speech recognition</h2><p>Turns your voice into words.</p></div><div class="stage-choice">${control("speech_model")}<p id="speech-stage-summary"></p><p id="speech-model-note">${esc(options.speech_model_note || rows.get("speech_model")[2])}</p><p id="speech-model-status" role="status"></p></div></section>
      <section class="stage-row" aria-labelledby="stage-cleanup"><div><h2 id="stage-cleanup">AI cleanup</h2><p>Tidies wording and spoken structure.</p></div><div class="stage-choice"><div class="setting-controls">${control("local_model")}</div><p id="model-status" role="status"></p><p id="model-benchmark-summary"></p><p>Runs locally. This text model is also used by any local Prompt Lab stage.</p><div class="cleanup-preference"><span>AI clean-up</span>${control("cleanup")}</div><details class="model-benchmarks"><summary>Benchmarks and recommendation</summary><div id="model-benchmark-content"></div></details></div></section>
      <section class="stage-row" aria-labelledby="stage-writing"><div><h2 id="stage-writing">Prompt writing</h2><p>Writes suggestions for your review.</p></div><div class="stage-choice">${control("engine")}<p data-route-summary="engine"></p></div></section>
      <section class="stage-row" aria-labelledby="stage-review"><div><h2 id="stage-review">Prompt review</h2><p>Assesses the current draft.</p></div><div class="stage-choice"><div class="setting-controls">${control("judge")}${control("judge_model")}</div><p data-route-summary="judge"></p></div></section>
    </div><h2 class="preference-heading">Recording preferences</h2>${["hotkey", "microphone", "live_preview"].map(row).join("")}
    <h2 class="preference-heading">Text and prompt preferences</h2>${["style", "target", "cloud_enabled"].map(row).join("")}`;
  $$("#settings-grid select").forEach(s => s.onchange = () => setSetting(s.dataset.key, s.value));
  $$("#settings-grid .switch").forEach(b => b.onclick = () => setSetting(b.dataset.toggle, !settings[b.dataset.toggle]));
  if (api().get_microphones) api().get_microphones().then(m => {
    options.microphone = m;
    const select = $('#settings-grid select[data-key="microphone"]');
    if (select) {
      select.innerHTML = Object.entries(m).map(([n, v]) => `<option value="${esc(v)}"${settings.microphone === v ? " selected" : ""}>${esc(n)}</option>`).join("");
      if (settings.microphone != null && !Object.values(m).includes(settings.microphone)) {
        const missing = new Option("Saved microphone unavailable — reconnect it or choose default", settings.microphone, true, true);
        select.add(missing);
      }
    }
  });
  api().diagnostics().then(d => { $("#diag").innerHTML = Object.entries(d).map(([k, v]) => `<div><b>${esc(k)}</b> · <span${k==="speech"?' id="diag-speech"':""}>${esc(v)}</span></div>`).join(""); });
  $("#refresh-models").onclick = refreshModels; refreshModels();
  const benchmarkDisclosure=$(".model-benchmarks");
  benchmarkDisclosure.ontoggle=()=>{if(benchmarkDisclosure.open)benchmarkDisclosure.scrollIntoView({block:"nearest"});};
  if(api().get_options) {
    const speechSelect = $("#speech-model"), version=speechOptionsVersion;
    api().get_options().then(latest=>{
      if(speechSelect!==$("#speech-model") || speechModelSavePending || version!==speechOptionsVersion)return;
      options=latest;
      const choices=speechChoices(); speechSelect.innerHTML=choices.html; speechSelect.dataset.available=String(choices.available); speechSelect.disabled=!choices.available;
      syncSettings();
    }).catch(()=>{ if(speechSelect===$("#speech-model")&&version===speechOptionsVersion&&!speechModelSavePending)$("#speech-model-status").textContent="Could not refresh speech choices. Your current model is preserved."; });
  }
  syncSettings();
  renderLearned();
}
function syncSettings() {
  $$("#settings-grid select[data-key]").forEach(s => { const v = String(settings[s.dataset.key] ?? ""); if (s.value !== v && [...s.options].some(o => o.value === v)) s.value = v; });
  $$("#settings-grid .switch").forEach(b => setSwitch(b, !!settings[b.dataset.toggle]));
  const m = $("#local-model"); if (m && settings.local_model && [...m.options].some(o => o.value === settings.local_model)) m.value = settings.local_model;
  $$('[data-route-summary]').forEach(el => {
    const key = el.dataset.routeSummary, local = settings[key] !== "anthropic";
    el.textContent = local ? `Uses ${settings.local_model || "the shared AI text model"} on this computer.` : settings.cloud_enabled ? "Cloud API enabled · paid calls. Text is sent only when you request this stage." : "Cloud selected, but cloud processing is disabled.";
  });
  const cloudJudge = $('#settings-grid select[data-key="judge_model"]');
  if (cloudJudge) cloudJudge.hidden = settings.judge !== "anthropic";
  const speechNote=$("#speech-model-note");if(speechNote&&options.speech_model_note)speechNote.textContent=options.speech_model_note;
}
let modelsRefreshVersion = 0, modelSavePending = false, modelChoiceUnconfirmed = false;
async function refreshModels() {
  const select = $("#local-model"), status = $("#model-status"), button = $("#refresh-models");
  if (!select) return;
  const version = ++modelsRefreshVersion;
  const current = () => version === modelsRefreshVersion && select === $("#local-model");
  select.disabled = true; button.disabled = true;
  if (modelSavePending) { status.textContent = "Saving model choice..."; return; }
  status.textContent = "Checking models available through Ollama...";
  try {
    const data = await api().get_local_models(); if (!current()) return;
    const selected = data.selected || settings.local_model || "";
    if (data.selected) { settings.local_model = data.selected; modelChoiceUnconfirmed = false; }
    const names = [...new Set((data.models || []).filter(n => typeof n === "string" && n))];
    const benchmark = globalThis.LISTEN_MODEL_BENCHMARKS;
    const tested = (benchmark?.models || []).filter(row=>names.includes(row.model)&&Number.isFinite(row.passed)&&Number.isFinite(row.cases)&&row.cases>0&&row.passed>=0&&row.passed<=row.cases&&Number.isFinite(row.medianSeconds)&&row.medianSeconds>=0).sort((a,b)=>b.passed/b.cases-a.passed/a.cases||a.medianSeconds-b.medianSeconds);
    const ranking = new Map(tested.map((row,index)=>[row.model,{...row,rank:index+1}]));
    if(tested.length)names.sort((a,b)=>(ranking.get(a)?.rank||Infinity)-(ranking.get(b)?.rank||Infinity)||a.localeCompare(b));
    const available = data.available === true || (data.available !== false && (names.length > 0 || !data.error));
    const choices = names.map(n => {
      const result=ranking.get(n), label=result ? `${n} · ${Math.round(100*result.passed/result.cases)}%${result.rank===1?" (Recommended)":""}` : `${n}${benchmark?" (Not benchmarked)":""}`;
      return new Option(label, n, n === selected, n === selected);
    });
    if (selected && !names.includes(selected)) {
      const saved = new Option(selected + (available ? " (saved; not installed)" : " (saved; availability not checked)"), selected, true, true);
      saved.disabled = true; choices.unshift(saved);
    }
    if (!choices.length) choices.push(new Option("No models available", ""));
    select.replaceChildren(...choices); select.disabled = !available || !names.length;
    select.onchange = async () => {
      if (modelSavePending || select.disabled || select !== $("#local-model")) return;
      modelSavePending = true; modelsRefreshVersion++;
      select.disabled = true; button.disabled = true; status.textContent = "Saving model choice...";
      try { await setSetting("local_model", select.value); }
      finally { modelSavePending = false; await refreshModels(); }
    };
    status.textContent = data.error || (names.length ? "Available in Ollama. Your saved choice is selected; Refresh does not download or run a model." : "No models are installed in Ollama. Refresh does not download a model.");
    const detail=$("#model-benchmark-content");
    const benchmarkSummary=$("#model-benchmark-summary");
    if(benchmarkSummary)benchmarkSummary.textContent=benchmark&&tested.length ? `Scores use the previous ${tested[0].cases}-sample cleanup test (${benchmark.recorded}). See the results and limits below.` : "No benchmark score is available for these choices.";
    if(detail)detail.innerHTML=benchmark ? `<p>Previous local cleanup test · ${esc(benchmark.recorded)}. Ranked by successful samples, then typical speed. The recommendation is for cleanup.</p><div class="benchmark-table-wrap"><table><thead><tr><th scope="col">Model</th><th scope="col">Successful samples</th><th scope="col">Typical time</th></tr></thead><tbody>${names.map(n=>{const row=ranking.get(n);return `<tr><th scope="row">${esc(n)}${row?.rank===1?' <span class="benchmark-recommended">Recommended</span>':""}</th><td>${row?`${row.passed}/${row.cases} · ${Math.round(100*row.passed/row.cases)}%`:"Not benchmarked"}</td><td>${row?`${row.medianSeconds.toFixed(2)} s`:"—"}</td></tr>`;}).join("")}</tbody></table></div><p>${esc(benchmark.method)}</p><p>${esc(benchmark.limitation)} Speed can vary with other apps and the selected speech model.</p>` : "<p>No recorded local benchmark is available. Refresh checks installed models only.</p>";
    syncSettings();
  } catch {
    if (!current()) return;
    const saved = new Option((settings.local_model || "No saved model") + (modelChoiceUnconfirmed ? " (last known; save not confirmed)" : " (saved; availability not checked)"), settings.local_model || "", true, true);
    saved.disabled = true; select.replaceChildren(saved);
    status.textContent = modelChoiceUnconfirmed ? "Model list unavailable. The last save was not confirmed; reconnect and refresh to check your choice." : "Model list unavailable. Your saved selection is preserved; start Ollama and try Refresh.";
  } finally { if (current()) button.disabled = false; }
}
async function renderLearned() {
  const L = await api().get_learned();
  setSwitch($("#learn-switch"), !!settings.learn);
  $("#learned").innerHTML = L.lines.length ? L.lines.map(l => `<div>${esc(l)}</div>`).join("") : `<div class="none">Nothing learned yet. Keep a few prompts (Paste, Copy, or Save in the lab) and correct generated text by hand; patterns show up here.${L.kept ? ` ${L.kept} kept so far.` : ""}</div>`;
  $("#learned-note").textContent = L.snippet && settings.learn ? "Added to the engineer prompt: " + L.snippet : "Built only from prompts you keep and corrections you make. Never changes code.";
}
$("#learn-switch").onclick = async () => { await setSetting("learn", !settings.learn); renderLearned(); };
$("#btn-reset-learned").onclick = () => clearPrivate("learned");

// ---------- vocabulary suggestions ----------
async function loadSuggestions() {
  const words = await api().get_vocab_suggestions();
  const previous = document.activeElement, action = previous?.dataset?.add !== undefined ? "add" : previous?.dataset?.no !== undefined ? "no" : null;
  const box = $("#vocab-suggest"); box.classList.toggle("hidden", !words.length);
  $("#vocab-chips").innerHTML = words.map(w => `<span class="chip" data-w="${esc(w)}">${esc(w)}<button class="add" title="Add to vocabulary" aria-label="Add ${esc(w)} to vocabulary" data-add="${esc(w)}">+</button><button class="no" title="Don't suggest again" aria-label="Stop suggesting ${esc(w)}" data-no="${esc(w)}">✕</button></span>`).join("");
  if (action) ($$("#vocab-chips [data-" + action + "]").find(b => b.dataset[action] === previous.dataset[action]) || vocab).focus();
  $$("#vocab-chips [data-add]").forEach(b => { b.disabled = vocabAdding || quitPending; b.onclick = () => addVocabSuggestion(b.dataset.add); });
  $$("#vocab-chips [data-no]").forEach(b => b.onclick = async () => { try { if(await api().dismiss_vocab(b.dataset.no)) await loadSuggestions(); else toast("Suggestion removal was not confirmed.", true); } catch { toast("Suggestion removal was not confirmed. Try again.", true); } });
}

// ---------- history & stats ----------
function fmtTs(ts) { const d = new Date(ts); return d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }); }
function wordLabel(n) { return n + (n === 1 ? " word" : " words"); }  // spelled out: a bare "w" reads as weeks
function histRow(e, i) {
  const outcome = { uncertain: "Check original app before copying again", retained: "Not sent - recover in Listen", copied: "Copied - paste manually", sent: "Text sent; arrival not checked", pasted: "Paste attempted; check original app", unknown: "Delivery status unavailable" }[e.delivery_status] || "";
  return `<li data-i="${i}"><span class="t">${fmtTs(e.ts)}</span>${e.kind === "prompt" ? '<span class="badge">prompt</span>' : e.kind === "engineer" ? '<span class="badge">Engineered prompt</span>' : ""}<span class="x">${esc(e.text)}${e.by ? `<small class="muted"> · ${esc(e.by)}</small>` : ""}${outcome ? `<small class="delivery-outcome">${outcome}</small>` : ""}</span><button class="btn sm" data-history-copy="clean">Copy</button>${e.raw ? '<button class="btn sm" data-history-copy="raw">Copy original</button>' : ""}<span class="n" title="Word count">${wordLabel(e.text.trim().split(/\s+/).filter(Boolean).length)}${e.cost ? ` · $${e.cost < 0.01 ? e.cost.toFixed(4) : e.cost.toFixed(2)}` : ""}</span></li>`;
}
// Rows keep their entries in memory; a row finds its entry by index instead of carrying copies of the text in the markup.
function bindRows(ul, rows) {
  $$("[data-history-copy]", ul).forEach(b => b.onclick = async () => { const e = rows[+b.closest("li").dataset.i]; try { if(await api().copy_text(b.dataset.historyCopy === "raw" ? (e.raw || "") : e.text)) toast("Copied"); else toast("Copy failed. Try again.",true); } catch { toast("Copy failed. Reconnect and try again.",true); } });
}
const HIST_PAGE = 200;
let histAll = [], histShown = [], recentShown = [], histLimit = HIST_PAGE, histTimer;
function renderHistory() {
  const q = $("#hist-search").value.trim().toLowerCase();
  const rows = histAll.filter(e => !q || e.text.toLowerCase().includes(q) || (e.raw || "").toLowerCase().includes(q));
  histShown = rows.slice(0, histLimit);
  $("#hist-list").innerHTML = rows.length ? histShown.map(histRow).join("") + (rows.length > histLimit ? `<li class="empty"><button class="link" id="hist-more">Show more (${rows.length - histLimit} more)</button></li>` : "") : q ? '<li class="empty">No matching dictations. <button class="link" id="clear-filter">Clear filter</button></li>' : '<li class="empty">Your dictations will appear here after you record.</li>';
  bindRows($("#hist-list"), histShown); if($("#clear-filter")) $("#clear-filter").onclick=()=> { $("#hist-search").value=""; histLimit=HIST_PAGE; renderHistory(); };
  if($("#hist-more")) $("#hist-more").onclick=()=> { histLimit+=HIST_PAGE; renderHistory(); $("#hist-more")?.focus(); };
}
async function loadHistory() {
  histAll = await api().get_history();
  renderHistory();
  const st = await api().get_stats();
  const stat = (v, l, s = "") => `<div class="stat"><div class="v">${v}</div><div class="l">${l}</div><div class="s">${s}</div></div>`;
  $("#stats").innerHTML = stat(st.today, "dictations today", `${st.week} this week`) + stat(st.words_week.toLocaleString(), "words this week", `${st.minutes_week.toFixed(1)} min of speech`) +
    stat(st.wpm || "–", "words per minute", "speaking rate") + stat(st.latency ? st.latency.toFixed(1) + "s" : "–", "Processing and delivery attempt", `$${st.spend_month.toFixed(2)} estimated API cost this month${st.spend_unknown ? ` · ${st.spend_unknown} unpriced calls` : ""}${st.spend_note ? " · " + esc(st.spend_note) : ""}`);
}
$("#hist-search").oninput = () => { clearTimeout(histTimer); histTimer = setTimeout(() => { histLimit = HIST_PAGE; renderHistory(); }, 200); };
async function clearPrivate(scope) {
  const descriptions = {history:"dictation history",workspace:"current and archived recovery drafts, instructions and pending suggestions",learned:"learned preferences",logs:"application logs"};
  if (!await askConfirm(`Clear ${descriptions[scope]}? Favorites and cost accounting remain. Older recovery backups may remain. This cannot be undone.`)) return;
  try { const result=await api().clear_private_data(scope,"DELETE"); if(scope==="history"||scope==="workspace")invalidatePreview(result.preview_version); toast(result.message || (result.ok ? "Cleared" : "Could not clear"),!result.ok); if(result.ok) { if(scope==="history") { loadHistory();loadRecent(); } if(scope==="workspace") { editorDirty=false;composerDirty=false;await loadLab(); } if(scope==="learned") renderLearned(); } } catch { toast("Clear was not confirmed. Reconnect and inspect before trying again.",true); }
}
$("#btn-clear-history").onclick = () => clearPrivate("history");
$$("[data-clear]").forEach(b=>b.onclick=()=>clearPrivate(b.dataset.clear));
async function loadRecent() {
  const history = await api().get_history();
  const rows = history.slice(0, 8);
  recentShown = rows;
  $("#recent-list").innerHTML = rows.length ? rows.map(histRow).join("") : '<li class="empty">Your dictations will show up here.</li>';
  bindRows($("#recent-list"), recentShown);
}

// ---------- prompt lab ----------
const ed = $("#draft"), composer = $("#composer");
ed.addEventListener("focus", () => editing = true);
ed.addEventListener("blur", () => { editing = false; flushEdit(); });
let composerTimer, composerVersion=0, composerDirty=false;
let editTimer, editVersion = 0, labRequest = 0, editorContext, editorDirty = false;
ed.addEventListener("input", () => {
  editing = true; editorDirty = true; editVersion++; clearTimeout(editTimer);
  const text = ed.value, context = editorContext, version = editVersion;
  editTimer = setTimeout(() => saveEdit(text, context, version), 300);
});
const KIND = { typed: "typed", engineer: "engineered", refine: "refined", improve: "improved", rebuild: "rebuilt", favorite: "favorite" };

async function loadLab() {
  const request = ++labRequest, version = editVersion, instructionVersion = composerVersion;
  const next = await api().get_lab();
  if (request !== labRequest || version !== editVersion || instructionVersion !== composerVersion) return request !== labRequest;  // false = owner typed mid-load: poll retries
  if (next.draft_id !== editorContext && (editorDirty || composerDirty)) {
    if (!api().retain_editor) return;
    if (editorDirty && !await api().retain_editor(ed.value)) return;
    if (composerDirty && !await api().retain_editor("Unsent instruction:\n" + composer.value)) return;
    if (request !== labRequest || version !== editVersion || instructionVersion !== composerVersion) return request !== labRequest;
    editorDirty = false; composerDirty = false;
    recovery("Unsaved work was kept in recovery before switching drafts.");
  }
  lab = next;
  editVersion = Math.max(editVersion, Number(next.edit_id) || 0);
  composerVersion = Math.max(composerVersion, Number(next.composer_edit_id) || 0);
  if (!editorDirty || next.draft_id !== editorContext) {
    setDisplayedProperty(ed, "value", lab.text || "");
    editorContext = lab.draft_id;
  }
  if (!composerDirty) setDisplayedProperty(composer, "value", lab.composer || "");
  recovery(editorDirty || composerDirty ? "Unsaved changes" : lab.save_error ? "Recovery could not be saved: " + lab.save_error : lab.saved ? "Saved locally" : "Recovery not yet saved");
  renderLabActivity(); renderCandidate(); loadArchives();
  ed.classList.toggle("busy", !!lab.busy);
  // meta line
  $("#lab-cost").textContent = lab.session_cost ? `Estimated session $${lab.session_cost.toFixed(3)}` : "";
  $("#draft-version").textContent = lab.cur >= 0 ? `v${lab.cur + 1}` : "";
  $("#btn-revisions").classList.toggle("hidden", !lab.revs.length);
  $("#draft-meta").innerHTML = lab.revs.length
    ? `v${lab.cur + 1} · ${esc(KIND[lab.revs[lab.cur]?.kind] || "")}${lab.by ? " by " + esc(lab.by) : ""}${lab.fav ? ` · favorite “${esc(lab.fav)}”` : ""}`
    : "Paste a prompt, type one, or dictate one here.";
  // revisions strip
  stableHTML($("#revs"), lab.revs.map(r => `<button class="rev ${r.i === lab.cur ? "cur" : ""}" data-i="${Number(r.i)}"${r.i === lab.cur ? ' aria-current="true"' : ""} title="${esc(KIND[r.kind] || r.kind)}${r.by ? " by " + esc(r.by) : ""}">v${Number(r.i) + 1}${r.score != null ? ` <b>${Number(r.score)}</b>` : ""}${r.stale ? ' <span class="stale">edited</span>' : ""}</button>`).join(""));
  $$("#revs .rev").forEach(b => b.onclick = async () => {
    if (!await flushAll()) return;
    editVersion++;
    await api().select_rev(+b.dataset.i);
    editing = false;
    await loadLab();
    const panel = $("#lab-revisions");
    if (panel.matches(":popover-open")) {
      const restoreFocus = panel.contains(document.activeElement) || document.activeElement === document.body;
      panel.hidePopover();
      if (restoreFocus) $("#btn-revisions").focus();
    }
  });
  // score bar
  const sb = $("#scorebar"), sc = lab.score;
  if (sc) {
    sb.classList.remove("hidden");
    const b = $("#score-badge"); b.textContent = `Model assessment ${sc.score} / 10${lab.stale ? " · edited" : ""}`; b.className = "score-badge " + (lab.stale ? "" : sc.score >= 8 ? "good" : sc.score >= 5 ? "" : "bad");
    const p = $("#score-prev");
    if (lab.prev_score == null) { p.textContent = ""; p.className = "score-prev muted"; }
    else { const d = sc.score - lab.prev_score; p.textContent = d === 0 ? `same as v${prevIdx() + 1}` : `${d > 0 ? "▲" : "▼"} ${Math.abs(d)} vs v${prevIdx() + 1} (${lab.prev_score})`; p.className = "score-prev " + (d > 0 ? "up" : d < 0 ? "down" : "muted"); }
    $("#score-gaps").textContent = `${sc.gaps.length} gap${sc.gaps.length === 1 ? "" : "s"} · ${sc.by}${sc.cost ? ` · $${sc.cost.toFixed(3)}` : ""}${sc.note ? " · " + sc.note : ""}`;
  } else sb.classList.add("hidden");
  // buttons
  const has = !!(lab.text || "").trim(), busy = !!lab.busy;
  $("#btn-improve").disabled = !has || busy; $("#btn-score").disabled = !has || busy; $("#btn-rebuild").disabled = !has || busy;
  $("#btn-send").disabled = busy || sending; composer.disabled = false;
  composer.placeholder = has ? "Describe what you want to change." : "Describe the prompt you want to write.";
  $("#lab-action-error").textContent=lab.error || ""; $("#lab-action-error").classList.toggle("hidden",!lab.error);
  if (lab.error && lab.error !== lastLabErr) { lastLabErr = lab.error; toast(lab.error, !/already as strong/.test(lab.error)); }
  if ($("#drawer").open) renderDrawer();
}
let lastLabErr = "";
function prevIdx() { for (let i = lab.cur - 1; i >= 0; i--) if (lab.revs[i].score != null) return i; return -1; }
function renderDrawer() {
  const sc = lab.score; if (!sc) return;

  $("#drawer-score").textContent = sc.score;
  $("#drawer-meta").textContent = `${sc.by}${sc.cost ? ` · $${sc.cost.toFixed(3)}` : ""}${sc.note ? "\n" + sc.note : ""}`;
  $("#drawer-criteria").innerHTML = Object.entries(sc.criteria).map(([k, v]) => `<div><span>${esc(k.replace("_", " "))}</span><b>${Number(v)}/5</b></div>`).join("");
  $("#drawer-gaps").innerHTML = sc.gaps.map(g => `<li>${esc(g)}</li>`).join("") || "<li>No gaps found.</li>";
  $("#drawer-history").innerHTML = lab.revs.filter(r => r.score != null).map(r => `<span${r.i === lab.cur ? ' style="color:var(--fg);border-color:var(--accent)"' : ""}>v${Number(r.i) + 1} · ${Number(r.score)}</span>`).join("") || "<span>no scores yet</span>";
}
let drawerOpener; function openDrawer() { if(!lab.score) return; drawerOpener=document.activeElement; renderDrawer(); $("#drawer").showModal(); }
$("#drawer").addEventListener("close",()=>drawerOpener?.focus());
$("#score-details").onclick = openDrawer;
$("#drawer-close").onclick = () => $("#drawer").close();
async function saveEdit(text, context, version) {
  recovery("Saving locally…");
  try { const saved=await api().set_text(text,context,version); if(version===editVersion && context===editorContext) { if(saved) editorDirty=false; recovery(saved ? "Saved locally" : "Draft not saved. Your text remains here; try again before continuing."); } return saved; }
  catch { recovery("Save not confirmed. Your text remains here; reconnect before continuing."); return false; }
}
async function flushEdit() { clearTimeout(editTimer); return !editorDirty || await saveEdit(ed.value,editorContext,editVersion); }
async function saveComposer(text,context,version) {
  recovery("Saving instruction…");
  try { const ok=await api().set_composer(text,context,version); if(version===composerVersion && context===editorContext) { if(ok) composerDirty=false; recovery(ok ? "Saved locally" : "Instruction not saved. Your input remains here."); } return ok; }
  catch { recovery("Instruction save not confirmed. Your input remains here."); return false; }
}
composer.addEventListener("input",()=> { composerDirty=true;composerVersion++;clearTimeout(composerTimer);const text=composer.value,context=editorContext,version=composerVersion; recovery("Unsaved instruction");composerTimer=setTimeout(()=>saveComposer(text,context,version),350); });
async function flushAll() { clearTimeout(composerTimer); if(!await flushEdit()) return false; return !composerDirty || await saveComposer(composer.value,editorContext,composerVersion); }
let quitPending = false;
window.requestDictationQuit = async () => {
  if (quitPending) return;
  if (vocabAdding) { toast("Vocabulary is still being updated. Wait for it to finish, then choose Quit again.", true); return; }
  quitPending = true;
  const fields = [ed, composer, vocab], readOnly = fields.map(el => el.readOnly);
  const context = editorContext, versions = [editVersion, composerVersion, vocabVersion], text = fields.map(el => el.value);
  fields.forEach(el => el.readOnly = true);
  let accepted = false;
  try {
    if (!await flushAll() || !await flushVocab()) { toast("Changes could not be saved. Keep the app open and try again.", true); return; }
    if (context !== editorContext || versions.some((v, i) => v !== [editVersion, composerVersion, vocabVersion][i]) || fields.some((el, i) => el.value !== text[i]) || editorDirty || composerDirty || vocabDirty) {
      toast("Your work changed while saving. Review it, then choose Quit again.", true); return;
    }
    accepted = !!api().quit_app && await api().quit_app() === true;
    if (!accepted) {
      const state = await api().get_state?.();
      toast(state?.last_error || "Listen is still working. Wait for the current operation to finish, then try Quit again.", true);
    }
  } catch (e) { toast("The app could not close. Try Quit again.", true); }
  finally { if (!accepted) { fields.forEach((el, i) => el.readOnly = readOnly[i]); quitPending = false; } }
};
$("#btn-quit").onclick = window.requestDictationQuit;
function positionLabPopover(panel) {
  const trigger = $(`[popovertarget="${panel.id}"]`).getBoundingClientRect();
  panel.style.left = Math.max(8, Math.min(trigger.left, innerWidth - panel.offsetWidth - 8)) + "px";
  panel.style.top = Math.max(8, Math.min(trigger.bottom + 6, innerHeight - panel.offsetHeight - 8)) + "px";
}
for (const panel of $$(".lab-popover")) {
  panel.addEventListener("toggle", ev => {
    if (ev.newState === "open") positionLabPopover(panel);
  });
}
// "More actions" menu: opens above its button, closes on outside click, on choosing an item, or on Escape (focus returns to the button).
const moreBtn = $("#btn-more"), moreMenu = $("#more-menu");
function closeMore(focus) {
  if (moreMenu.classList.contains("hidden")) return false;
  moreMenu.classList.add("hidden"); moreBtn.setAttribute("aria-expanded", "false"); if (focus) moreBtn.focus();
  return true;
}
function positionMore() {
  const r = moreBtn.getBoundingClientRect();
  moreMenu.style.left = Math.max(8, Math.min(r.left, innerWidth - moreMenu.offsetWidth - 8)) + "px";
  moreMenu.style.bottom = (innerHeight - r.top + 6) + "px";
  moreMenu.style.maxHeight = Math.max(40, r.top - 14) + "px";
}
moreBtn.onclick = () => {
  if (closeMore(false)) return;
  moreMenu.classList.remove("hidden"); moreBtn.setAttribute("aria-expanded", "true");
  positionMore();
};
window.addEventListener("resize", () => {
  for (const panel of $$(".lab-popover:popover-open")) positionLabPopover(panel);
  if (!moreMenu.classList.contains("hidden")) positionMore();
});
moreMenu.addEventListener("click", ev => {
  const button = ev.target.closest("button");
  if (button && button.id !== "btn-delete-draft") closeMore(true);
});
document.addEventListener("click", ev => { if (!ev.target.closest(".more-actions, dialog")) closeMore(false); });
for(const [id,method] of [["improve","improve"],["score","score"],["rebuild","rebuild"]]) $("#btn-"+id).onclick=async()=> {
  if(method!=="score" && suggestionWaiting())return;
  if(!await flushAll())return;
  if(method!=="score" && suggestionWaiting())return;
  try { if(!await api()[method]()) toast("Action not started. Check processing choice or wait for current work.",true); }
  catch { toast("Result not confirmed. Check the draft before explicitly trying again.",true); }
};
let sending=false;
function suggestionWaiting() {
  if(!lab.pending)return false;
  setDisplayedProperty($("#lab-action-error"), "textContent", "Open Compare suggestion to review it, then apply or discard it before requesting another.");
  $("#lab-action-error").classList.remove("hidden");
  renderLabActivity();
  return true;
}
function renderLabActivity() {
  const message = lab.busy === "scoring" ? "Scoring draft…"
    : lab.busy || sending ? "Writing suggestion…"
    : lab.pending ? "Suggestion ready" : "";
  setDisplayedProperty($("#lab-busy"), "textContent", message);
  $("#lab-busy").dataset.ready = String(message === "Suggestion ready");
  $("#btn-review").classList.toggle("hidden", !lab.pending);
}
async function send() {
  if(sending || lab.busy)return;
  if(suggestionWaiting())return;
  const text=composer.value.trim(), version=composerVersion, context=editorContext;
  if(!text) { $("#lab-action-error").textContent="Describe what you want in Instruction, then choose Write suggestion."; $("#lab-action-error").classList.remove("hidden"); composer.focus(); return; }
  sending=true;$("#btn-send").disabled=true;renderLabActivity();
  try {
    if(!await flushAll())return;
    if(suggestionWaiting())return;
    if(context!==editorContext || version!==composerVersion || text!==composer.value.trim()) {
      recovery("The draft or instruction changed. Nothing was sent; review your current work before sending.");
      return;
    }
    if(!await api().submit(text,context)) { recovery("Instruction was not sent. Your input remains here.");return; }
    recovery("Instruction accepted. It stays recoverable until a suggestion is ready.");
    await loadLab().catch(() => false); // Polling retries a failed refresh after submission.
  } catch { recovery("Send not confirmed. Your instruction remains here. Check results before sending again."); }
  finally {sending=false;$("#btn-send").disabled=!!lab.busy;renderLabActivity();}
}
$("#btn-send").onclick=send;
composer.addEventListener("keydown",ev=> { if(ev.key==="Enter"&&!ev.shiftKey&&!ev.ctrlKey&&!ev.altKey&&!ev.metaKey&&!ev.isComposing&&ev.keyCode!==229) {ev.preventDefault();send();} });
$("#btn-copy").onclick=async()=> {if(!ed.value.trim()||!await flushAll())return;try {if(await api().copy_text(ed.value)) {api().lab_kept();toast("Copied");} else toast("Copy failed",true);}catch{toast("Copy not confirmed",true);} };
$("#btn-paste").onclick=async()=> {if(!ed.value.trim()||!await flushAll())return;try{await api().paste_draft(ed.value);}catch{toast("Paste not confirmed. Check your destination before trying again.",true);} };
$("#btn-new").onclick=async()=> {if(!await flushAll())return;try{if(await api().new_draft()){editVersion++;await loadLab();$("#drawer").close();ed.focus();}else toast("Could not create a draft; your text remains.",true);}catch{toast("New draft not confirmed; your text remains.",true);} };
function askFavoriteName() {const dialog=$("#favorite-name-dialog"),input=$("#favorite-name"),opener=document.activeElement;if(dialog.open)return Promise.resolve(null);input.value=lab.fav||"";dialog.returnValue="";return new Promise(resolve=>{dialog.addEventListener("close",()=>{opener?.focus();resolve(dialog.returnValue==="save"?input.value.trim():null);},{once:true});dialog.showModal();input.focus();});}
$("#btn-save").onclick=async()=> {if(!ed.value.trim()||!await flushAll())return;const name=await askFavoriteName();if(!name)return;try {let result=await api().save_favorite_checked(name,ed.value,false);if(result.status==="conflict"&&await askConfirm(`Replace favorite “${name}”? Its existing text will be replaced. The previous version remains in archived recovery drafts.`))result=await api().save_favorite_checked(name,ed.value,true);if(result.status==="saved"){toast("Favorite saved");loadFavs();}else if(result.status==="error")toast("Favorite could not be saved",true);}catch{toast("Favorite save not confirmed. Inspect Library before trying again.",true);} };
const reviewDialog = $("#candidate"), reviewButton = $("#btn-review");
let reviewId, resolvingCandidate = false;
function renderCandidate() {
  if (!reviewDialog.open) return;
  const pending = lab.pending;
  if (pending?.id !== reviewId) { reviewDialog.close(); return; }
  setDisplayedProperty($("#candidate-original"), "value", pending.source_text);
  setDisplayedProperty($("#candidate-text"), "value", pending.text);
  const stale = pending.stale || pending.source_text !== ed.value || editorDirty;
  $("#btn-apply").disabled = stale || !!lab.busy || resolvingCandidate;
  $("#btn-discard").disabled = resolvingCandidate;
  $("#candidate-note").classList.toggle("hidden", !stale);
  setDisplayedProperty($("#candidate-note"), "textContent", stale
    ? "This suggestion is for an earlier draft. Discard it before requesting a new suggestion for your current draft." : "");
  setDisplayedProperty($("#candidate-version"), "textContent", !stale && lab.cur >= 0 ? `Draft v${lab.cur + 1}` : "");
  setDisplayedProperty($("#candidate-model"), "textContent", pending.by || "Selected model");
}
function reviewError(message) {
  const error = $("#candidate-error");
  error.textContent = message;
  error.classList.toggle("hidden", !message);
}
reviewButton.onclick = () => {
  if (!lab.pending || reviewDialog.open) return;
  reviewId = lab.pending.id;
  $("#review-show-suggestion").checked = true;
  reviewError("");
  reviewDialog.showModal();
  renderCandidate();
};
$("#btn-close-review").onclick = () => reviewDialog.close();
reviewDialog.addEventListener("close", () => {
  if (page === "lab") (lab.pending ? reviewButton : ed).focus();
});
ed.addEventListener("input", renderCandidate);
async function resolveCandidate(apply) {
  if (!reviewDialog.open || resolvingCandidate || lab.pending?.id !== reviewId) return;
  const id = reviewId;
  resolvingCandidate = true;
  reviewError("");
  renderCandidate();
  try {
    if (apply && !await flushAll()) {
      reviewError("Your edits could not be saved. Return to the draft and try again.");
      return;
    }
    const resolved = apply ? await api().apply_candidate(id) : await api().discard_candidate(id);
    await loadLab();
    if (!resolved && reviewDialog.open && reviewId === id) {
      reviewError(apply ? "Could not apply this suggestion. Your draft is preserved." : "Could not discard this suggestion. Try again.");
    }
  } catch {
    if (reviewDialog.open && reviewId === id) reviewError("The change could not be confirmed. Return to the draft and check the result.");
  } finally {
    resolvingCandidate = false;
    renderCandidate();
  }
}
$("#btn-apply").onclick = () => resolveCandidate(true);
$("#btn-discard").onclick = () => resolveCandidate(false);
async function loadArchives(){try{const rows=await api().list_drafts(),el=$("#archived-drafts");const html='<option value="">Choose a draft</option>'+rows.map(r=>`<option value="${esc(r.id)}">${esc(r.title)}</option>`).join("");stableHTML(el,html);}catch{/* recovery remains visible */}}
$("#btn-restore").onclick=async()=> {const id=$("#archived-drafts").value;if(!id||!await flushAll())return;try{if(await api().restore_draft(id)){await loadLab();}else toast("Draft could not be restored",true);}catch{toast("Restore not confirmed. Your input remains.",true);} };
$("#btn-delete-draft").onclick=async()=> {const id=$("#archived-drafts").value;if(!id||!await askConfirm("Delete the selected archived draft? Current work and cost accounting remain. Older recovery backups may still contain it."))return;try{if(await api().delete_draft(id,"DELETE")){await loadArchives();toast("Archived draft deleted. Recovery backups may remain.");}else toast("Draft deletion was not saved.",true);}catch{toast("Deletion not confirmed. Inspect the list before retrying.",true);} };
$("#btn-library").onclick = () => { const l = $("#library"); l.classList.toggle("hidden"); if (!l.classList.contains("hidden")) loadFavs(); };
async function loadFavs() {
  favs=await api().get_favorites();
  const previous = document.activeElement, action = previous?.dataset?.name !== undefined ? "name" : previous?.dataset?.remove !== undefined ? "remove" : null;
  $("#fav-list").innerHTML=favs.length ? favs.map(f=>`<li><button class="link favorite-load" data-name="${esc(f.name)}">${esc(f.name)}</button><span class="x muted">${esc(f.text.slice(0,80))}</span><button class="btn sm" data-remove="${esc(f.name)}" aria-label="Remove ${esc(f.name)}">Remove</button></li>`).join("") : '<li class="empty">No favorites yet. Save a draft to reuse it here.</li>';
  if (action) $$("#fav-list [data-" + action + "]").find(b => b.dataset[action] === previous.dataset[action])?.focus();
  $$(".favorite-load").forEach(b=>b.onclick=async()=> {
    if(!await flushAll())return;
    try { if(await api().load_favorite(b.dataset.name)) { await loadLab(); const active = document.activeElement, focus = page === "lab" && $("#fav-list").contains(active) && active.dataset.name === b.dataset.name; $("#library").classList.add("hidden"); if(focus) ed.focus(); } }
    catch {toast("Favorite load not confirmed",true);}
  });
  $$("[data-remove]").forEach(b=>b.onclick=async()=> {
    try {
      const token=await api().remove_favorite(b.dataset.remove); if(!token)return toast("Favorite could not be removed",true);
      const focused=await loadFavs(),undo=document.createElement("button"),undoBox=$("#favorite-undo");
      undo.className="btn";undo.textContent="Undo removal";
      undo.onclick=async()=>{
        try {
          if(await api().undo_favorite_removal(token)) { await loadFavs(); const focus = page === "lab" && document.activeElement === undo; undoBox.replaceChildren(); undoBox.classList.add("hidden"); if(focus) $$("#fav-list .favorite-load").find(el=>el.dataset.name===b.dataset.remove)?.focus(); toast("Favorite restored"); }
          else toast("Could not restore: that name may already exist. Your newer favorite is preserved.",true);
        } catch { toast("Restoration was not confirmed. Try Undo again.",true); }
      };
      undoBox.replaceChildren(undo);undoBox.classList.remove("hidden");if(focused === b && document.activeElement === document.body) undo.focus();toast("Favorite removed. Undo is available.");
    } catch {toast("Removal not confirmed",true);}
  });
  return previous;
}

// ---------- vocabulary ----------
const vocab = $("#vocab"); let vocabTimer, vocabDirty = false, vocabVersion = 0, vocabSave = Promise.resolve(true), vocabAdding = false;
function flushVocab() {
  clearTimeout(vocabTimer);
  const version = vocabVersion, text = vocab.value;
  const save = async () => {
    if (!vocabDirty) return true;
    if (version !== vocabVersion) return false;
    setDisplayedProperty($("#vocab-status"), "textContent", "Saving vocabulary...");
    try {
      const saved = await api().set_setting("vocab", text) === true;
      if (version !== vocabVersion) return false;
      if (saved) { settings.vocab = text; vocabDirty = false; }
      setDisplayedProperty($("#vocab-status"), "textContent", saved ? "Vocabulary saved locally" : "Vocabulary not saved. Your words remain here; keep Listen open and try again.");
      return saved;
    } catch {
      if (version === vocabVersion) setDisplayedProperty($("#vocab-status"), "textContent", "Vocabulary save not confirmed. Your words remain here; keep Listen open.");
      return false;
    }
  };
  vocabSave = vocabSave.then(save, save);
  return vocabSave;
}
vocab.oninput = () => { vocabDirty = true; vocabVersion++; clearTimeout(vocabTimer); setDisplayedProperty($("#vocab-status"), "textContent", "Unsaved vocabulary"); vocabTimer = setTimeout(flushVocab, 400); };
async function addVocabSuggestion(word) {
  if (vocabAdding || quitPending) return;
  vocabAdding = true;
  const readOnly = vocab.readOnly;
  vocab.readOnly = true;
  $$("#vocab-chips [data-add]").forEach(b => b.disabled = true);
  try {
    if (!await flushVocab()) return;
    const version = vocabVersion;
    setDisplayedProperty($("#vocab-status"), "textContent", "Adding vocabulary...");
    if (!await api().add_vocab(word)) { setDisplayedProperty($("#vocab-status"), "textContent", "Vocabulary change was not confirmed. Your words remain here."); return; }
    const latest = await api().get_settings();
    if (version === vocabVersion && !vocabDirty) { settings = latest; vocab.value = latest.vocab; setDisplayedProperty($("#vocab-status"), "textContent", "Vocabulary saved locally"); }
    toast(`Added “${word}”`); await loadSuggestions().catch(() => toast("Vocabulary added. Suggestions could not refresh.", true));
  } catch { setDisplayedProperty($("#vocab-status"), "textContent", "Vocabulary change was not confirmed. Your words remain here; check Settings before trying again."); }
  finally { vocabAdding = false; vocab.readOnly = readOnly; $$("#vocab-chips [data-add]").forEach(b => b.disabled = quitPending); }
}

// ---------- command palette ----------
const commands = () => [
  { l: "Go to Dictate", k: "Ctrl+1", f: () => show("dictate") }, { l: "Go to Prompt Lab", k: "Ctrl+2", f: () => show("lab") },
  { l: "Go to History", k: "Ctrl+3", f: () => show("history") }, { l: "Go to Settings", k: "Ctrl+4", f: () => show("settings") },
  { l: "Improve prompt", f: () => $("#btn-improve").click() }, { l: "Get feedback", f: () => $("#btn-score").click() },
  { l: "Paste draft", k: "Ctrl+Enter", f: () => $("#btn-paste").click() }, { l: "Save favorite", k: "Ctrl+S", f: () => $("#btn-save").click() }, { l: "New draft", f: () => $("#btn-new").click() },
  ...["hotkey", "target", "engine", "judge", "judge_model", "style"].flatMap(k => Object.entries(options[k] || {}).map(([n, v]) => ({ l: `${SETTING_ROWS.find(r => r[0] === k)[1]}: ${n}`, f: () => setSetting(k, v) }))),
  { l: `AI clean-up: ${settings.cleanup ? "turn off" : "turn on"}`, f: () => setSetting("cleanup", !settings.cleanup) },
];
let palSel = 0, paletteOpener = null, paletteItems = [];
function updatePaletteSelection() {
  $$("#palette-list li").forEach((li, i) => {
    const selected = i === palSel;
    li.classList.toggle("sel", selected);
    li.setAttribute("aria-selected", String(selected));
  });
  const input = $("#palette-input"), selected = $("#palette-list .sel");
  if (selected) {
    input.setAttribute("aria-activedescendant", selected.id);
    selected.scrollIntoView({block: "nearest"});
  } else input.removeAttribute("aria-activedescendant");
}
function renderPalette() {
  const q = $("#palette-input").value.toLowerCase();
  const items = commands().filter(c => c.l.toLowerCase().includes(q)).slice(0, 12);
  paletteItems = items;
  palSel = Math.min(Number.isFinite(palSel) ? palSel : 0, Math.max(0, items.length - 1));
  $("#palette-list").innerHTML = items.map((c, i) => `<li id="palette-option-${i}" role="option" aria-selected="${i === palSel}" class="${i === palSel ? "sel" : ""}" data-i="${i}"><span>${esc(c.l)}</span><span class="muted">${c.k || ""}</span></li>`).join("");
  updatePaletteSelection();
  $("#palette-empty").classList.toggle("hidden", !!items.length);
  $$("#palette-list li").forEach(li => li.onclick = () => { const command = items[+li.dataset.i]; closePalette(); command.f(); });
  return items;
}
function openPalette() { paletteOpener = document.activeElement; $("#palette-input").value = ""; palSel = 0; renderPalette(); $("#palette").showModal(); updatePaletteSelection(); $("#palette-input").setAttribute("aria-expanded", "true"); $("#palette-input").focus(); }
function closePalette() { $("#palette").close(); $("#palette-input").setAttribute("aria-expanded", "false"); if (paletteOpener?.isConnected) paletteOpener.focus(); }
$("#palette").addEventListener("cancel", ev => { ev.preventDefault(); closePalette(); });
$("#palette-input").oninput = () => { palSel = 0; renderPalette(); };
$("#palette").onclick = ev => { if (ev.target.id === "palette") closePalette(); };

// ---------- keyboard ----------
document.addEventListener("keydown", ev => {
  if (ev.defaultPrevented || ev.isComposing || ev.keyCode === 229) return;
  const pal = $("#palette").open;
  if(!pal && document.querySelector('dialog[open]')) return;
  if (pal) {
    const items = paletteItems;
    if (ev.key === "ArrowDown") { if(items.length)palSel = (palSel + 1) % items.length; updatePaletteSelection(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { if(items.length)palSel = (palSel - 1 + items.length) % items.length; updatePaletteSelection(); ev.preventDefault(); }
    else if (ev.key === "Enter") { const command=items[palSel]; if(command){closePalette();command.f();} ev.preventDefault(); }
    else if (ev.key === "Escape") {closePalette();ev.preventDefault();}
    return;
  }
  if (ev.ctrlKey && ev.key >= "1" && ev.key <= "4") { show(["dictate", "lab", "history", "settings"][+ev.key - 1]); ev.preventDefault(); }
  else if (ev.ctrlKey && ev.key.toLowerCase() === "k") { openPalette(); ev.preventDefault(); }
  else if (ev.ctrlKey && !ev.altKey && !ev.metaKey && !ev.shiftKey && ev.key === "Enter" && page === "lab") { $("#btn-paste").click(); ev.preventDefault(); }
  else if (ev.ctrlKey && ev.key.toLowerCase() === "s" && page === "lab") { $("#btn-save").click(); ev.preventDefault(); }
  else if (ev.key === "Escape") { closeMore(true); $("#drawer").close(); $("#library").classList.add("hidden"); }
});

// ---------- polling ----------
function setDisplayedProperty(element, property, value) {
  if (element[property] !== value) element[property] = value;
}
// Under 600px tall the banners collapse to one summary line; syncBanners keeps that line current.
function syncBanners() {
  const shown = $$("#banners > .notice, #banners > .card").filter(el => !el.classList.contains("hidden")), first = shown[0];
  $("#banners-bar").classList.toggle("hidden", !shown.length);
  if (!shown.length) $("#banners").classList.toggle("open", false);
  const text = first ? (first.classList.contains("notice") ? first.textContent : first.querySelector(".card-title")?.textContent || "") : "";
  setDisplayedProperty($("#banners-summary"), "textContent", text + (shown.length > 1 ? ` (+${shown.length - 1} more)` : ""));
}
function setStatus(st) {
  const s = st.last_error && st.state !== "recording" && !st.busy ? "error" : st.state === "recording" ? "recording" : st.busy || (st.state === "loading" ? "loading" : "idle");
  const recordingStarting = s === "recording" && st.recording_ready === false;
  const stoppingPreview=st.state!=="recording"&&st.preview_busy&&!st.busy;
  const cls = stoppingPreview ? "busy" : s === "recording" ? recordingStarting ? "busy" : "recording" : s === "idle" ? "idle" : s === "error" ? "error" : s === "loading" ? "" : "busy";
  const recordingLabel = st.recording_ready == null ? "recording" : st.recording_ready ? "listening" : "starting…";
  const label = stoppingPreview ? st.pending_dictations>0 ? "finishing dictation…" : "stopping preview…" : s === "recording" ? recordingLabel + (st.locked ? " · hands-free" : "") : s === "loading" ? (st.last_error ? "failed to start" : "loading…") : s;
  const keyName = Object.entries(options.hotkey || {}).find(([, value]) => value === settings.hotkey)?.[0] || "the dictation key";
  setDisplayedProperty($("#dictate-heading"), "textContent", s === "recording" ? recordingStarting ? "Starting…" : "Listening" : s === "loading" ? "Getting ready…" : s === "error" ? "Needs your attention" : st.busy || stoppingPreview ? "Finishing your words…" : "Ready when you are");
  setDisplayedProperty($("#dictate-instruction"), "textContent", s === "recording" ? recordingStarting ? "Opening your microphone. Wait for the ready cue." : st.locked ? `Tap ${keyName} or choose Finish recording. Esc cancels.` : "Release the dictation key to finish. Esc cancels." : s === "loading" ? "Loading speech. Your saved words remain available." : `Hold ${keyName} to speak. Release to send your words, or tap for hands-free.`);
  for (const el of $$(".status")) {
    setDisplayedProperty($(".dot", el), "className", "dot " + cls);
    setDisplayedProperty($(".status-text", el), "textContent", label + (settings.cleanup && !st.llm_ok && s === "idle" ? st.cleanup_warming ? " · AI clean-up loading…" : " · AI clean-up offline" : ""));
  }
  const announcement=$("#recording-announcement");if(announcement.textContent!==label)announcement.textContent=label;
  setDisplayedProperty($("#rail-status .dot"), "className", "dot " + cls);
  setDisplayedProperty($("#rail-status"), "title", label);
  document.body.classList.toggle("recording", s === "recording");
  document.body.classList.toggle("recording-starting", recordingStarting);
  setDisplayedProperty($("#btn-finish"), "disabled", s!=="recording");
  setDisplayedProperty($("#btn-cancel"), "disabled", s!=="recording");
  $("#btn-finish").classList.toggle("primary", s==="recording");
  const speech=st.speech_runtime||{}, speechNotice=$("#speech-runtime-status");
  const speechMessage=speech.reason==="preview_recovery_pending" ? "Live preview paused. Speech will recover when you finish recording, or choose Retry startup when idle." : speech.state==="failed" ? "Speech is unavailable. Finish or cancel any recording, then choose Retry startup." : speech.state==="switching" ? (speech.device==="cpu" ? "Switching speech to your computer's processor. Please wait." : "Loading local speech. You can keep using the window.") : speech.fallback ? "Speech is using your computer's processor because GPU acceleration is unavailable. Your selected speech model is unchanged; dictation may take longer." : "";
  speechNotice.classList.toggle("hidden",!speechMessage);
  if(speechNotice.textContent!==speechMessage)speechNotice.textContent=speechMessage;
  $("#btn-retry").classList.toggle("hidden",!((st.state==="loading"&&st.last_error)||speech.state==="failed"||speech.reason==="preview_recovery_pending"));
  setDisplayedProperty($("#btn-retry"), "disabled", !(st.speech_retry_ready??(st.state==="loading"&&!!st.last_error)));
  const speechDiagnostic=$("#diag-speech");
  if(speechDiagnostic&&st.speech_summary&&speechDiagnostic.textContent!==st.speech_summary)speechDiagnostic.textContent=st.speech_summary;
  const speechSelect=$("#speech-model"), speechStatus=$("#speech-model-status");
  if(speechSelect?.tagName==="SELECT") {
    const pending=speechModelSavePending||speechSelect.dataset.saving==="true";
    setDisplayedProperty(speechSelect,"disabled",pending||!!st.speech_model_switching||st.state==="recording"||!!st.busy||!!st.lab_busy||!!st.pending_dictations||!!st.preview_busy||(st.state==="loading"&&!st.last_error)||speechSelect.dataset.available==="false");
    if(st.speech_model_switching||pending)setDisplayedProperty(speechStatus,"textContent","Changing speech model…");
    else if(!pending)setDisplayedProperty(speechStatus,"textContent","");
    if(!pending&&!st.speech_model_switching&&st.speech_runtime?.model&&[...speechSelect.options].some(option=>option.value===st.speech_runtime.model))setDisplayedProperty(speechSelect,"value",st.speech_runtime.model);
  }
  const speechSummary=$("#speech-stage-summary");if(speechSummary&&st.speech_summary)setDisplayedProperty(speechSummary,"textContent",st.speech_summary);
  syncBanners();
}
let lastErr = "", retainedVersion = -1;
async function loadRetained() {
  if (!api().get_retained) return;
  const rows = await api().get_retained(), box = $("#retained-results");
  box.classList.toggle("hidden", !rows.length);
  $("#retained-list").innerHTML = rows.map(r => `<div class="retained-item"><p>${esc(r.message)}</p><textarea class="input" readonly aria-label="Unpasted text">${esc(r.text)}</textarea><button class="btn" data-copy-retained="${esc(r.id)}">Copy text</button> <button class="btn" data-dismiss-retained="${esc(r.id)}">Dismiss</button></div>`).join("");
  $$('[data-copy-retained]').forEach(b => b.onclick = async () => { try { if(await api().copy_retained(+b.dataset.copyRetained)) toast("Copied. Paste into your intended app."); else toast("This result changed or could not be copied. Your visible text remains available to select and copy.",true); } catch { toast("Copy was not confirmed. Your text remains here; reconnect and try again.",true); } });
  $$('[data-dismiss-retained]').forEach(b => b.onclick = async () => { try { if(!await api().dismiss_retained(+b.dataset.dismissRetained))toast("Dismissal could not be saved. The recovery text is preserved.",true); await loadRetained(); } catch { toast("Dismissal was not confirmed. The recovery text remains available.",true); } });
}
let manualClipboardReceipt=null;
async function loadDeliveryStatus() {
  if(!api().get_delivery_status)return;
  const state=await api().get_delivery_status();
  manualClipboardReceipt=state.manual_id || null;
  $("#delivery-recovery").classList.toggle("hidden",!state.pending);
  setDisplayedProperty($("#delivery-title"), "textContent", manualClipboardReceipt ? "Previous clipboard item kept" : "Paste needs confirmation");
  setDisplayedProperty($("#delivery-description"), "textContent", manualClipboardReceipt
    ? "Your previous clipboard item is kept here. Normal dictation can continue. After pasting your text, restore the previous item or choose to keep your current clipboard. Keep Listen open until you decide."
    : "Check the destination before copying again; the text may already be there. New dictations are saved in recovery while this paste is unresolved. Keep Listen open to preserve your previous clipboard.");
  setDisplayedProperty($("#delivery-message"), "textContent", state.message || "");
  $$('[data-copy-retained]').forEach(b=>b.disabled=!!state.pending&&!manualClipboardReceipt);
}
$("#restore-clipboard").onclick=async()=> {
  const receipt=manualClipboardReceipt;
  if(!await askConfirm(receipt ? "After pasting your dictation, restore your previous clipboard item?" : "First wait for the receiving app to finish pasting, or close that app. Restore your previous clipboard only when no paste is still waiting; otherwise a delayed paste could insert old text."))return;
  try {const result=await api().restore_delivery_clipboard(true,receipt);toast(result.message,result.status!=="restored");await loadDeliveryStatus();}
  catch {toast("Clipboard restoration was not confirmed. The saved clipboard remains in memory; keep Listen open.",true);}
};
$("#discard-clipboard").onclick=async()=> {
  const receipt=manualClipboardReceipt;
  if(!await askConfirm(receipt ? "Keep your current clipboard and discard the saved previous item?" : "First finish or cancel the waiting paste, or close the receiving app. Keep your current clipboard and permanently discard the saved previous clipboard?"))return;
  try {const result=await api().discard_delivery_clipboard(true,receipt);toast(result.message,result.status!=="resolved");await loadDeliveryStatus();}
  catch {toast("Clipboard recovery is still pending. Keep Listen open and try again.",true);}
};
async function poll() {
  pollRunning=true;
  const statePreviewEpoch=previewUiEpoch;
  try {
    S = await api().get_state(); bridgeLost=false;connection("");
  } catch (e) {
    bridgeLost=true;connection("Desktop connection lost. Your input remains here. Reconnect the app before continuing; no action will retry automatically.");
  }
  if (!bridgeLost) try {
    setStatus(S);
    renderPreview(S,statePreviewEpoch);
    if(!S.busy && S.state!=="recording" && Date.now()-lastDeliveryAt>=1000) { lastDeliveryAt=Date.now(); await loadDeliveryStatus(); }
    if (S.retained_ver !== retainedVersion) { await loadRetained(); retainedVersion = S.retained_ver; }
    if (S.levels && S.levels.length) drawBars(S.levels); else if (!document.body.classList.contains("recording")) drawBars([]);
    if (S.lab_ver !== lastLabVer) { if (await loadLab() !== false) lastLabVer = S.lab_ver; }  // an aborted load is retried next tick
    if (S.lab_busy !== lastBusy) { if (lastBusy && !S.lab_busy && lab.score && !lab.error) toast(`v${lab.cur + 1} scored ${lab.score.score}/10 by ${lab.score.by}`); lastBusy = S.lab_busy; }
    if (S.history_len !== lastHistLen) { lastHistLen = S.history_len; loadRecent(); loadSuggestions(); if (page === "history") loadHistory(); }
    if (S.event === "show_lab") show("lab");
    if (S.event === "pasted") toast("Pasted");
    if (S.event === "sent") toast("Text sent");
    if (S.last_error && S.last_error !== lastErr) { lastErr = S.last_error; toast(S.last_error, true); }
  } catch (e) { toast("Could not refresh part of the screen. Your input is preserved.", true); }
  pollRunning=false; pollTimer=setTimeout(poll, pollDelay());
}
let lastDeliveryAt=0, pollTimer=0, pollRunning=false;
const pollDelay = () => document.hidden ? 1000 : document.body.classList.contains("recording") ? 33 : 250;

// ---------- boot ----------
for(const [id,method] of [["finish","finish_recording"],["cancel","cancel_recording"],["retry","retry_startup"]]) $("#btn-"+id).onclick=async()=> {const endsPreview=id!=="retry";if(endsPreview){previewActionPending++;invalidatePreview();}try{if(!await api()[method]())toast("Action unavailable. Check status and try again.",true);}catch{connection("Desktop connection lost. Your text is preserved.");}finally{if(endsPreview){previewActionPending--;invalidatePreview();}}};
async function boot() {
  if(booted)return;booted=true;
  $("#demo-banner").classList.toggle("hidden",!demo);
  try{settings=await api().get_settings();options=await api().get_options();vocab.value=settings.vocab||"";renderChips();await loadLab();await loadRecent();show("dictate",false);poll();}
  catch{booted=false;connection("Could not connect to the desktop app. Your input is preserved. Reopen the app to reconnect.");}
}
// Hidden windows poll slowly; coming back restores the normal rate at once (unless a poll is already running).
document.addEventListener("visibilitychange",()=> { if(!document.hidden&&pollTimer&&!pollRunning) { clearTimeout(pollTimer); poll(); } });
$("#banners-toggle").onclick=()=> { const open=$("#banners").classList.toggle("open"); $("#banners-toggle").setAttribute("aria-expanded",String(open)); $("#banners-toggle").textContent=open?"Hide":"Details"; };
window.addEventListener("pywebviewready",()=>{bridgeReady=true;boot();},{once:true});
if(demo || bridgeReady)boot();else connection("Connecting to desktop app…");
})();
