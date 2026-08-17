"""Client-facing voice picker for the K O'Brien demo dashboard.

Lets the client (Keith) preview voices, fine-tune tone, and set one LIVE on his
own assistant. Reuses existing infra:
  - GET  /client/api/voices        -> fresh Vapi 11labs library + Irish account voices
  - GET  /client/api/voice-preview -> ElevenLabs TTS of the greeting w/ tone sliders (audio)
  - POST /client/api/voice         -> _vapi_safe_patch the tenant's assistant voice (tools preserved)
client.html gains a "Voice" tab. Bounded to the tenant's own assistant_ids (check_client).

Non-destructive: anchors asserted present exactly once; refuses if already applied.
Backups: *.bak-obrien-voice for --rollback.

  python scripts/apply-obrien-voicepicker.py --check
  python scripts/apply-obrien-voicepicker.py
  python scripts/apply-obrien-voicepicker.py --rollback
"""
import argparse, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE / "server.py"
CLIENT = HERE / "client.html"
MARK = "obrien-voice"

# ---------------- server.py: 3 endpoints inserted before /client/api/calls ----------------
SRV_ANCHOR = '@app.get("/client/api/calls")'
SRV_BLOCK = '''# --- obrien-voice: client-facing voice picker ---
OBRIEN_IRISH_VOICES = [
    {"id": "eyuCA3LWMylRajljTeOo", "name": "Gerry", "tagline": "Warm Derry tradesman", "accent": "Irish", "gender": "male"},
    {"id": "LhG6Tsjmn5tklSCyReiu", "name": "Conor", "tagline": "Warm, grounded Irish", "accent": "Irish", "gender": "male"},
    {"id": "U3AWuAe8WcVA50PuDMrY", "name": "Cillian", "tagline": "Deep, warm, calm", "accent": "Irish", "gender": "male"},
    {"id": "kOvUpYLYS0rKGldsKcD1", "name": "Maeve", "tagline": "Soft Irish female", "accent": "Irish", "gender": "female"},
]
_OBRIEN_GREETING = "Hi, you've reached K O'Brien Heating and Plumbing. How can I help you today?"


def _client_assistant_ids(c):
    raw = c.get("assistant_ids") or []
    if isinstance(raw, str):
        raw = [s.strip() for s in raw.strip("{}").split(",") if s.strip()]
    return list(raw)


@app.get("/client/api/voices")
async def client_voices(token: str = Query("")):
    c = check_client(token)
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    el = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    voices = []
    try:
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.get("https://api.vapi.ai/voice-library/11labs",
                            headers={"Authorization": f"Bearer {vk}"})
        for v in (r.json() if r.status_code == 200 else []):
            voices.append({"id": v.get("providerId") or v.get("slug"), "name": v.get("name"),
                           "tagline": (v.get("description") or "")[:60],
                           "accent": (v.get("accent") or "").title(), "gender": v.get("gender") or "",
                           "preview": v.get("previewUrl") or ""})
    except Exception:
        pass
    have = {v["id"] for v in voices}
    for iv in reversed(OBRIEN_IRISH_VOICES):  # Irish voices to the front
        if iv["id"] in have:
            continue
        prev = ""
        if el:
            try:
                async with httpx.AsyncClient(timeout=15) as h:
                    rr = await h.get(f"https://api.elevenlabs.io/v1/voices/{iv['id']}",
                                     headers={"xi-api-key": el})
                prev = rr.json().get("preview_url", "") if rr.status_code == 200 else ""
            except Exception:
                pass
        voices.insert(0, {**iv, "preview": prev})
    current = ""
    ids = _client_assistant_ids(c)
    if ids:
        try:
            async with httpx.AsyncClient(timeout=15) as h:
                ar = await h.get(f"https://api.vapi.ai/assistant/{ids[0]}",
                                 headers={"Authorization": f"Bearer {vk}"})
            current = (ar.json().get("voice") or {}).get("voiceId", "")
        except Exception:
            pass
    for v in voices:
        v["current"] = (v["id"] == current)
    return {"voices": voices, "current": current}


@app.get("/client/api/voice-preview")
async def client_voice_preview(token: str = Query(""), voice_id: str = Query(""),
                               stability: float = Query(0.5), similarity: float = Query(0.75),
                               style: float = Query(0.45), text: str = Query("")):
    check_client(token)
    el = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not voice_id or not el:
        raise HTTPException(status_code=400, detail="voice_id and eleven key required")
    say = (text or _OBRIEN_GREETING)[:300]
    body = json.dumps({"text": say, "model_id": "eleven_flash_v2_5",
                       "voice_settings": {"stability": stability, "similarity_boost": similarity,
                                          "style": style, "use_speaker_boost": True}}).encode()
    async with httpx.AsyncClient(timeout=60) as h:
        r = await h.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}", content=body,
                         headers={"xi-api-key": el, "Content-Type": "application/json",
                                  "Accept": "audio/mpeg"})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"tts failed {r.status_code}")
    return Response(content=r.content, media_type="audio/mpeg")


@app.post("/client/api/voice")
async def client_set_voice(request: Request, token: str = Query("")):
    c = check_client(token)
    ids = _client_assistant_ids(c)
    if not ids:
        raise HTTPException(status_code=400, detail="no assistant for tenant")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    voice_id = (body.get("voice_id") or "").strip()
    if not voice_id:
        raise HTTPException(status_code=400, detail="voice_id required")
    voice_patch = {"provider": "11labs", "voiceId": voice_id, "model": "eleven_flash_v2_5",
                   "stability": float(body.get("stability", 0.5)),
                   "similarityBoost": float(body.get("similarity", 0.75)),
                   "style": float(body.get("style", 0.45)),
                   "useSpeakerBoost": True, "cachingEnabled": False}
    vk = os.environ.get("VAPI_API_KEY", "").strip()
    applied = []
    for aid in ids:
        try:
            async with httpx.AsyncClient(timeout=20) as h:
                r = await h.get(f"https://api.vapi.ai/assistant/{aid}",
                                headers={"Authorization": f"Bearer {vk}"})
            cur = (r.json().get("voice") or {})
            await _vapi_safe_patch(aid, {"voice": {**cur, **voice_patch}})
            applied.append(aid)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"voice set failed: {str(e)[:120]}")
    try:
        await send_telegram(f"\U0001F399 {c.get('tenant_slug')} set voice {voice_id} on {len(applied)} assistant(s)")
    except Exception:
        pass
    return {"ok": True, "voice_id": voice_id, "assistants": applied}


'''

# ---------------- client.html edits ----------------
CLI_TAB_ANCHOR = '''    <button class="tab" data-tab="settings" aria-selected="false" onclick="navigate('settings')">Settings</button>'''
CLI_TAB_NEW = CLI_TAB_ANCHOR + '''
    <button class="tab" data-tab="voice" aria-selected="false" onclick="navigate('voice')">Voice</button>'''

CLI_VIEW_ANCHOR = '''  </main>'''
CLI_VIEW_NEW = '''      <!-- Voice view (obrien-voice) -->
      <section id="voiceView" class="view">
        <div class="panel">
          <div class="panel-header">
            <div><h2 class="panel-title">Choose your voice</h2>
            <p class="panel-subtitle">Preview a voice, tune the tone, then set it live on your line.</p></div>
            <button class="btn" type="button" onclick="loadVoices()">Refresh</button>
          </div>
          <div id="voiceTuner" style="padding:10px 6px;border-bottom:1px solid var(--border);font-size:12px;color:var(--muted);">
            Fine-tune tone (applies to preview + when you set a voice):
            <label style="margin-left:10px;">Stability <input id="vStab" type="range" min="0" max="1" step="0.05" value="0.5"></label>
            <label style="margin-left:10px;">Expressiveness <input id="vStyle" type="range" min="0" max="1" step="0.05" value="0.45"></label>
            <label style="margin-left:10px;">Clarity <input id="vSim" type="range" min="0" max="1" step="0.05" value="0.75"></label>
          </div>
          <div class="panel-body"><div id="voiceList" class="empty">Loading voices…</div></div>
        </div>
      </section>
''' + CLI_VIEW_ANCHOR

CLI_NAV_ANCHOR = '''  if (tab === "settings") loadSettings();'''
CLI_NAV_NEW = CLI_NAV_ANCHOR + '''
  if (tab === "voice") loadVoices();  // obrien-voice'''

CLI_JS_ANCHOR = '''window.login = login;'''
CLI_JS_NEW = '''// --- obrien-voice: client voice picker ---
function voiceTone(){
  const g=(id,d)=>{const e=document.getElementById(id);return e?parseFloat(e.value):d;};
  return {stability:g('vStab',0.5),style:g('vStyle',0.45),similarity:g('vSim',0.75)};
}
async function loadVoices(){
  const el=qs("#voiceList"); el.innerHTML='<div class="empty">Loading voices…</div>';
  try{
    const d=await api("/client/api/voices");
    state.voices=(d&&d.voices)||[];
    if(!state.voices.length){el.innerHTML='<div class="empty">No voices available.</div>';return;}
    el.innerHTML=state.voices.map(renderVoiceCard).join("");
  }catch(e){el.innerHTML='<div class="empty">'+esc(e.message)+'</div>';}
}
function renderVoiceCard(v){
  const cur=v.current?' <span class="badge ok" style="font-size:10px;padding:2px 7px;border-radius:9px;">current</span>':'';
  const meta=[v.accent,v.gender,v.tagline].filter(Boolean).map(esc).join(' · ');
  return '<div style="display:grid;grid-template-columns:1fr auto auto;gap:10px;align-items:center;padding:12px 6px;border-bottom:1px solid var(--border);">'
    +'<div><strong>'+esc(v.name||v.id)+'</strong>'+cur+'<div class="muted" style="font-size:12px;margin-top:2px;">'+meta+'</div></div>'
    +'<button class="btn" type="button" onclick="previewVoice(\\''+esc(v.id)+'\\')">▶ Preview</button>'
    +'<button class="btn btn-primary" type="button" onclick="applyVoice(\\''+esc(v.id)+'\\',\\''+esc((v.name||v.id).replace(/'/g,"")) +'\\')">Use this voice</button>'
    +'</div>';
}
let _voicePrev=null;
function previewVoice(id){
  if(_voicePrev){try{_voicePrev.pause();}catch(_){}}
  const t=voiceTone();
  const url='/client/api/voice-preview?token='+encodeURIComponent(state.token)+'&voice_id='+encodeURIComponent(id)
           +'&stability='+t.stability+'&similarity='+t.similarity+'&style='+t.style;
  _voicePrev=new Audio(url);
  _voicePrev.play().catch(()=>{ const v=(state.voices||[]).find(x=>x.id===id); if(v&&v.preview){_voicePrev=new Audio(v.preview);_voicePrev.play();} });
}
async function applyVoice(id,name){
  const t=voiceTone();
  try{
    await api("/client/api/voice",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({voice_id:id,stability:t.stability,similarity:t.similarity,style:t.style})});
    alert("Your line now uses "+name+".");
    loadVoices();
  }catch(e){alert("Could not set that voice: "+e.message);}
}
window.loadVoices = loadVoices;
window.previewVoice = previewVoice;
window.applyVoice = applyVoice;
window.login = login;'''

SRV_EDITS = [(SRV_ANCHOR, SRV_BLOCK + SRV_ANCHOR)]
CLI_EDITS = [
    (CLI_TAB_ANCHOR, CLI_TAB_NEW),
    (CLI_VIEW_ANCHOR, CLI_VIEW_NEW),
    (CLI_NAV_ANCHOR, CLI_NAV_NEW),
    (CLI_JS_ANCHOR, CLI_JS_NEW),
]


def check_file(path, edits):
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "ALREADY", []
    problems = [f"  count={text.count(o)} need 1: {o[:55]!r}" for o, _ in edits if text.count(o) != 1]
    return ("OK" if not problems else "FAIL"), problems


def apply_file(path, edits):
    text = path.read_text(encoding="utf-8")
    bak = path.with_suffix(path.suffix + ".bak-obrien-voice")
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def rollback(path):
    bak = path.with_suffix(path.suffix + ".bak-obrien-voice")
    if bak.exists():
        path.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8"); bak.unlink()
        print(f"  restored {path.name}")
    else:
        print(f"  no backup for {path.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    a = ap.parse_args()
    if a.rollback:
        print("[rollback]"); rollback(SERVER); rollback(CLIENT); return 0
    sst, sp = check_file(SERVER, SRV_EDITS)
    cst, cp = check_file(CLIENT, CLI_EDITS)
    print("server.py :", sst); [print(x) for x in sp]
    print("client.html:", cst); [print(x) for x in cp]
    if "ALREADY" in (sst, cst):
        print("Already applied. --rollback to revert."); return 0
    if "FAIL" in (sst, cst):
        print("FAIL: anchors not clean. No writes."); return 1
    if a.check:
        print("[check] anchors OK."); return 0
    apply_file(SERVER, SRV_EDITS); apply_file(CLIENT, CLI_EDITS)
    print("Applied. Backups: *.bak-obrien-voice")
    return 0


if __name__ == "__main__":
    sys.exit(main())
