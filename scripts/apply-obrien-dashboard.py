"""Dashboard v1 patch for the K O'Brien demo (spec: owner opens it and instantly
sees who called, why, how urgent, and can filter).

Surfaces the structured_data already stored by /vapi/call-ended and renders it:
  - server.py  /client/api/calls  -> expose urgency, category, gas_emergency,
                                      new_or_existing, location, eircode
  - client.html                    -> urgency/GAS badge (red, unmistakable),
                                      category + new/existing, filter row
                                      (All / Urgent / New / Existing)

Non-destructive: every anchor is asserted present exactly once before any write;
refuses if already applied (marker). Keeps <file>.bak-obrien-dash for --rollback.

  python scripts/apply-obrien-dashboard.py --check     # verify anchors, no write
  python scripts/apply-obrien-dashboard.py             # apply
  python scripts/apply-obrien-dashboard.py --rollback  # restore .bak
"""
import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SERVER = HERE / "server.py"
CLIENT = HERE / "client.html"
MARK = "obrien-dash"

# ---- server.py: surface structured_data in the calls list ----
SRV_OLD = '''        grouped[cid] = {
            "call_id": cid,
            "ts": str(r["ts"]),
            "caller_name": d.get("name") or d.get("caller_name") or "",
            "caller_phone": d.get("contact_phone") or d.get("phone") or d.get("caller") or "",'''
SRV_NEW = '''        sd = d.get("structured_data") or {}  # obrien-dash
        grouped[cid] = {
            "call_id": cid,
            "ts": str(r["ts"]),
            "caller_name": d.get("name") or d.get("caller_name") or sd.get("customer_name") or "",
            "caller_phone": d.get("contact_phone") or d.get("phone") or d.get("caller") or sd.get("phone") or "",
            "urgency": (sd.get("urgency") or "").lower(),
            "category": sd.get("category") or "",
            "gas_emergency": bool(sd.get("gas_emergency")),
            "new_or_existing": sd.get("new_or_existing") or "",
            "location": sd.get("location") or "",
            "eircode": sd.get("eircode") or "",'''

# ---- client.html: helpers inserted before renderCallRow ----
CLI_HELPERS_ANCHOR = "function renderCallRow(c) {"
CLI_HELPERS = '''// obrien-dash helpers
function prettyCat(c){const m={emergency_gas:'Gas emergency',boiler_heating_breakdown:'Boiler/heating',plumbing_problem:'Plumbing',boiler_service_maintenance:'Service',installation_new_work:'Installation',quote_request:'Quote',existing_customer_job:'Existing job',general_enquiry:'Enquiry',other:'Other'};return m[c]||c;}
function urgencyChip(c){
  if(c.gas_emergency) return '<span class="badge" style="background:#c0261b;color:#fff;font-weight:800;font-size:11px;padding:3px 9px;border-radius:10px;letter-spacing:.3px;">\\u26A0 SUSPECTED GAS</span>';
  const u=(c.urgency||'');
  if(u==='urgent') return '<span class="badge" style="background:#c0261b;color:#fff;font-weight:800;font-size:11px;padding:3px 9px;border-radius:10px;">URGENT</span>';
  if(u==='priority') return '<span class="badge" style="background:#e08a00;color:#fff;font-weight:700;font-size:11px;padding:3px 9px;border-radius:10px;">PRIORITY</span>';
  return '';
}
function renderCalls(){
  const f=state.callFilter||'all';
  let rows=state.calls||[];
  if(f==='urgent') rows=rows.filter(c=>c.gas_emergency||c.urgency==='urgent'||c.urgency==='priority');
  else if(f==='new') rows=rows.filter(c=>(c.new_or_existing||'')==='new');
  else if(f==='existing') rows=rows.filter(c=>(c.new_or_existing||'')==='existing');
  const el=document.querySelector('#callsList');
  el.innerHTML = rows.length ? rows.map(renderCallRow).join("") : '<div class="empty">No calls match this filter.</div>';
}
function setCallFilter(f,btn){state.callFilter=f; if(btn){[...btn.parentElement.children].forEach(b=>b.classList.remove('btn-primary')); btn.classList.add('btn-primary');} renderCalls();}
function renderCallRow(c) {'''

# ---- client.html: urgency chip into the row badge column ----
CLI_ROW_OLD = '''      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
        <span class="badge ${badge.cls}" style="font-size:11px;padding:3px 8px;border-radius:10px;">${esc(badge.label)}</span>
      </div>'''
CLI_ROW_NEW = '''      <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;">
        ${urgencyChip(c)}
        <span class="badge ${badge.cls}" style="font-size:11px;padding:3px 8px;border-radius:10px;">${esc(badge.label)}</span>
        ${c.category ? '<span class="muted" style="font-size:10px;">'+esc(prettyCat(c.category))+(c.new_or_existing?' \\u00B7 '+esc(c.new_or_existing):'')+'</span>' : ''}
      </div>'''

# ---- client.html: filter row before the calls list ----
CLI_FILTER_ANCHOR = '''        <div class="panel-body" id="callsList">'''
CLI_FILTER_NEW = '''        <div id="callFilters" style="display:flex;gap:6px;flex-wrap:wrap;padding:0 6px 10px;">
          <button class="btn btn-primary" type="button" onclick="setCallFilter('all',this)">All</button>
          <button class="btn" type="button" onclick="setCallFilter('urgent',this)">Urgent</button>
          <button class="btn" type="button" onclick="setCallFilter('new',this)">New enquiries</button>
          <button class="btn" type="button" onclick="setCallFilter('existing',this)">Existing</button>
        </div>
        <div class="panel-body" id="callsList">'''

# ---- client.html: route the two render paths through renderCalls ----
CLI_R1_OLD = '''    qs("#callsList").innerHTML = state.calls.map(renderCallRow).join("");'''
CLI_R1_NEW = '''    renderCalls();  // obrien-dash'''
CLI_R2_OLD = '''      qs("#callsList").innerHTML = state.calls.length
        ? state.calls.map(renderCallRow).join("")
        : '<div class="empty">No calls match that search.</div>';'''
CLI_R2_NEW = '''      renderCalls();  // obrien-dash'''

# ---- client.html: expose setCallFilter on window ----
CLI_WIN_ANCHOR = "window.loadCalls = loadCalls;"
CLI_WIN_NEW = "window.loadCalls = loadCalls;\nwindow.setCallFilter = setCallFilter;  // obrien-dash"

SRV_EDITS = [(SRV_OLD, SRV_NEW)]
CLI_EDITS = [
    (CLI_HELPERS_ANCHOR, CLI_HELPERS),
    (CLI_ROW_OLD, CLI_ROW_NEW),
    (CLI_FILTER_ANCHOR, CLI_FILTER_NEW),
    (CLI_R1_OLD, CLI_R1_NEW),
    (CLI_R2_OLD, CLI_R2_NEW),
    (CLI_WIN_ANCHOR, CLI_WIN_NEW),
]


def check_file(path, edits):
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        return "ALREADY", []
    problems = []
    for old, _new in edits:
        n = text.count(old)
        if n != 1:
            problems.append(f"  anchor count={n} (need 1): {old[:60]!r}…")
    return ("OK" if not problems else "FAIL"), problems


def apply_file(path, edits):
    text = path.read_text(encoding="utf-8")
    bak = path.with_suffix(path.suffix + ".bak-obrien-dash")
    if not bak.exists():
        bak.write_text(text, encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, f"anchor not unique in {path.name}"
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def rollback(path):
    bak = path.with_suffix(path.suffix + ".bak-obrien-dash")
    if bak.exists():
        path.write_text(bak.read_text(encoding="utf-8"), encoding="utf-8")
        bak.unlink()
        print(f"  restored {path.name}")
    else:
        print(f"  no backup for {path.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    args = ap.parse_args()

    if args.rollback:
        print("[rollback]")
        rollback(SERVER); rollback(CLIENT)
        return 0

    sst, sp = check_file(SERVER, SRV_EDITS)
    cst, cp = check_file(CLIENT, CLI_EDITS)
    print(f"server.py : {sst}")
    for p in sp: print(p)
    print(f"client.html: {cst}")
    for p in cp: print(p)

    if "ALREADY" in (sst, cst):
        print("\nAlready applied (marker present). --rollback to revert.")
        return 0
    if "FAIL" in (sst, cst):
        print("\nFAIL: anchors did not match cleanly. No files written.")
        return 1
    if args.check:
        print("\n[check] all anchors OK. Re-run without --check to apply.")
        return 0

    apply_file(SERVER, SRV_EDITS)
    apply_file(CLIENT, CLI_EDITS)
    print("\nApplied. Backups: *.bak-obrien-dash")
    return 0


if __name__ == "__main__":
    sys.exit(main())
