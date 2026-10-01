/* Admin-only mini dashboard. Existing admin API helper supplies authentication. */
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {personal:'Personal', sales:'CallMeIE sales', support:'CallMeIE support', other:'Other', spam:'Spam', new:'New', follow_up:'Follow-up required', contacted:'Contacted', waiting:'Waiting', closed:'Closed'};
  const time = value => value ? new Date(value).toLocaleString('en-IE', {day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) : '—';
  const options = (values, selected) => values.map(v => `<option value="${esc(v)}" ${v === selected ? 'selected' : ''}>${esc(labels[v] || v)}</option>`).join('');
  const phone = value => /^\+?[0-9 ()-]{5,25}$/.test(value || '') ? value.replace(/[ ()-]/g, '') : '';
  let root, api, toast, data, tab = 'inbox', selected = '', detail = null, filters = {q:'', kind:'', status:''};
  let loading = false, mounted = false, generation = 0;
  const base = '/admin/api/personal-agent';

  function flash(message, type = 'success') { if (toast) toast(message, type); }
  async function request(path, method = 'GET', body) {
    return api(base + path, {method, ...(body ? {headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {})});
  }
  async function refresh() {
    const run = ++generation;
    try {
      const next = await request('');
      if (run !== generation) return;
      data = next;
      render();
    } catch (err) { root.innerHTML = `<div class="empty">Personal agent could not load. ${esc(err.message)} <button class="btn" data-pa="refresh">Retry</button></div>`; }
  }
  function header() {
    return `<div class="pa-header"><div><h1 class="panel-title">Personal agent</h1><p class="panel-subtitle">Adam’s personal and CallMeIE calls · <a href="tel:${esc(data.number)}">061 788 358</a></p></div><button class="btn" data-pa="sync">Sync calls</button></div>
      <div class="pa-status"><span><strong>${data.stats.open}</strong> open</span><span><strong>${data.stats.urgent}</strong> urgent</span><span><strong>${data.stats.total}</strong> calls</span><span>SMS alerts: ${data.live_notification_channel === 'sms' ? 'on' : 'off'}</span><span>${data.revision === data.published_revision ? 'Agent configuration published' : 'Unpublished changes'}</span></div>
      <nav class="pa-tabs" aria-label="Personal agent workspace">${[['inbox','Follow-ups'],['calls','All calls'],['workflow','Workflow'],['knowledge','Knowledge'],['settings','Notifications']].map(([key,title]) => `<button type="button" data-pa="tab" data-value="${key}" class="${tab === key ? 'active' : ''}" aria-pressed="${tab === key}">${title}</button>`).join('')}</nav>`;
  }
  function callRows() {
    const rows = data.calls.filter(c => (tab !== 'inbox' || c.status !== 'closed') && (!filters.kind || (c.intake.kind || 'other') === filters.kind) && (!filters.status || c.status === filters.status) && (!filters.q || JSON.stringify(c).toLowerCase().includes(filters.q.toLowerCase())));
    return `<div class="pa-filters"><input class="input" id="pa-search" placeholder="Search caller, number or message" aria-label="Search personal calls" value="${esc(filters.q)}"><select class="input" id="pa-kind" aria-label="Call type"><option value="">All types</option>${options(['personal','sales','support','other','spam'],filters.kind)}</select><select class="input" id="pa-filter-status" aria-label="Follow-up status"><option value="">All statuses</option>${options(['new','follow_up','contacted','waiting','closed'],filters.status)}</select><button class="btn" data-pa="filter">Filter</button></div>
      <div class="pa-workspace"><div class="pa-list">${rows.length ? rows.map(c => `<button class="pa-call ${selected === c.call_id ? 'selected' : ''}" data-pa="open" data-value="${esc(c.call_id)}"><div class="pa-call-heading"><strong>${esc(c.intake.caller_name || 'Unknown caller')}</strong><span class="pa-tag ${c.intake.urgency === 'urgent' ? 'urgent' : ''}">${esc(c.intake.urgency === 'urgent' ? 'Urgent' : labels[c.intake.kind || 'other'])}</span></div><p>${esc(c.intake.reason || c.summary || 'Review call transcript')}</p><div class="pa-call-meta"><span>${esc(time(c.ts))}</span><span>${esc(c.caller_phone || 'No callback number')}</span><span>${esc(labels[c.status])}</span></div>${c.next_action ? `<div class="pa-next">Next: ${esc(c.next_action)}</div>` : ''}</button>`).join('') : `<div class="empty">${tab === 'inbox' ? 'No open follow-ups.' : 'No matching calls.'}</div>`}</div><section class="pa-inspector" aria-label="Call context">${detail ? inspector() : '<div class="empty">Select a call to review its message, add notes and follow up.</div>'}</section></div>`;
  }
  function inspector() {
    const c = detail, s = c.state, i = s.intake;
    const number = phone(i.callback_number || data.calls.find(x => x.call_id === selected)?.caller_phone);
    const previous = number ? data.calls.filter(x => x.call_id !== selected && phone(x.caller_phone) === number).slice(0,5) : [];
    return `<div class="pa-detail-head"><div><h2>${esc(i.caller_name || 'Call context')}</h2><p class="muted">${esc(labels[i.kind || 'other'])} · ${esc(i.urgency || 'normal')}</p></div><button class="btn" data-pa="close-detail" aria-label="Close call context">Close</button></div><p class="pa-message">${esc(i.reason || c.summary || 'No confirmed message captured. Review transcript.')}</p>
      <dl class="pa-facts"><dt>Callback</dt><dd>${esc(i.callback_number || number || 'Not captured')}</dd><dt>Requested action</dt><dd>${esc(i.requested_action || 'Not specified')}</dd><dt>Preferred callback</dt><dd>${esc(i.preferred_callback || 'Not specified')}</dd>${i.company ? `<dt>Company</dt><dd>${esc(i.company)}</dd>` : ''}${i.service ? `<dt>Affected service</dt><dd>${esc(i.service)}</dd>` : ''}${i.impact ? `<dt>Impact</dt><dd>${esc(i.impact)}</dd>` : ''}</dl>
      <div class="pa-actions">${number ? `<a class="btn primary" href="tel:${esc(number)}">Call back</a><a class="btn" href="sms:${esc(number)}">Text caller</a><button class="btn" data-pa="copy" data-value="${esc(number)}">Copy number</button>` : '<span class="muted">A callback number was not captured.</span>'}</div>
      <form id="pa-followup" class="pa-form"><h3>Follow-up</h3><label>Status<select class="input" name="status">${options(['new','follow_up','contacted','waiting','closed'],s.status)}</select></label><label>Next action<input class="input" name="next_action" maxlength="1000" value="${esc(s.next_action)}" placeholder="What do you need to do?"></label><label>Follow-up due<input class="input" name="due_at" type="datetime-local" value="${esc(localDate(s.due_at))}"></label><button class="btn primary" type="submit">Save follow-up</button></form>
      <section class="pa-notes"><h3>Notes</h3>${c.notes.length ? c.notes.map(n => `<article class="pa-note"><small>${esc(time(n.created_at))} · ${esc(n.actor)}</small><p>${esc(n.note)}</p></article>`).join('') : '<p class="muted">No notes yet.</p>'}<form id="pa-note"><label class="sr-only" for="pa-note-text">Internal note</label><textarea id="pa-note-text" class="textarea" name="note" rows="3" maxlength="4000" placeholder="Add context or record what you did…" required></textarea><button class="btn" type="submit">Save note</button></form></section>
      ${previous.length ? `<section class="pa-notes"><h3>Previous calls from this number</h3><p class="muted">For your context. A matching number does not verify identity.</p>${previous.map(x => `<button class="pa-call" data-pa="open" data-value="${esc(x.call_id)}"><small>${esc(time(x.ts))} · ${esc(labels[x.status])}</small><p>${esc(x.intake.reason || x.summary)}</p></button>`).join('')}</section>` : ''}
      <details class="pa-transcript"><summary>Transcript and call timeline</summary><p class="muted">Transcription may contain errors. Caller details should be confirmed.</p><pre>${esc(c.transcript || 'No transcript stored.')}</pre><ol>${c.events.map(e => `<li><small>${esc(time(e.ts))}</small> ${esc(e.type.replaceAll('-',' '))}<p>${esc(e.summary)}</p></li>`).join('')}</ol></details>`;
  }
  function localDate(value) {
    if (!value) return '';
    const d = new Date(value);
    if (!Number.isFinite(d.getTime())) return '';
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0,16);
  }
  function configPanel() {
    const c = data.config;
    let body = '';
    if (tab === 'workflow') body = `<h2>Message-taking workflow</h2><p class="muted">Personal messages, sales enquiries and support faults. No appointment booking.</p><label>Greeting<textarea class="textarea" name="greeting" rows="3" maxlength="500">${esc(c.greeting)}</textarea></label><label>Call handling instructions<textarea class="textarea" name="workflow" rows="10" maxlength="8000">${esc(c.workflow)}</textarea></label><div class="pa-rule">Always confirm the callback number. Save before ending. The agent cannot transfer, book appointments or promise response times.</div>`;
    if (tab === 'knowledge') body = `<h2>Knowledge and context</h2><p class="muted">Only approved information is shared with the voice agent. Keep private context in operator notes.</p><label>Approved caller-facing information<textarea class="textarea" name="public_knowledge" rows="10" maxlength="8000">${esc(c.public_knowledge)}</textarea></label><label>Private operator context<textarea class="textarea" name="private_notes" rows="5" maxlength="8000" placeholder="Your own context. Never sent to the voice agent.">${esc(c.private_notes)}</textarea></label><div class="pa-rule">Do not add passwords, financial details, resident records or private contact information to caller-facing knowledge.</div>`;
    if (tab === 'settings') body = `<h2>Notifications</h2><p class="muted">Alerts use your existing owner destination. Every alert is labelled Personal agent.</p><label>Channel<select class="input" name="notification_channel"><option value="off" ${c.notification_channel === 'off' ? 'selected' : ''}>Off</option><option value="sms" ${c.notification_channel === 'sms' ? 'selected' : ''}>SMS to configured owner number</option><option value="telegram" ${c.notification_channel === 'telegram' ? 'selected' : ''}>Existing owner Telegram chat</option></select></label><label>Alert policy<select class="input" name="notification_mode"><option value="all" ${c.notification_mode === 'all' ? 'selected' : ''}>All messages; urgent messages immediately</option><option value="urgent" ${c.notification_mode === 'urgent' ? 'selected' : ''}>Urgent messages only</option></select></label><label>Lock-screen message detail<select class="input" name="notification_preview"><option value="minimal" ${c.notification_preview === 'minimal' ? 'selected' : ''}>Minimal: name and call type</option><option value="full" ${c.notification_preview === 'full' ? 'selected' : ''}>Full: reason and callback number</option></select></label><div class="pa-rule">Routine alerts follow call completion. Urgent alerts follow message capture. Spam is saved without an alert. Provider acceptance is not proof of handset delivery.</div>`;
    return `<div class="pa-config"><form id="pa-config-form" class="pa-form">${body}<div class="pa-actions"><button class="btn" type="submit">Save draft</button><button class="btn primary" type="submit" name="publish" value="yes">Save and activate</button>${tab === 'settings' ? '<button class="btn" type="button" data-pa="test-notification">Send test alert</button>' : ''}</div><p class="muted">Saved version ${data.revision} · live version ${data.published_revision}</p></form>${tab === 'settings' ? `<section class="pa-delivery"><h3>Recent delivery attempts</h3><button class="btn" type="button" data-pa="check-delivery">Check delivery status</button>${data.notifications.length ? data.notifications.slice(0,12).map(n => `<div class="pa-delivery-row"><span>${esc(time(n.updated_at))} · ${esc(n.channel)}</span><strong>${esc(n.status)}</strong>${n.error ? `<small>${esc(n.error)}</small>` : ''}</div>`).join('') : '<p class="muted">No delivery attempts yet.</p>'}</section>` : ''}</div>`;
  }
  function render() {
    root.innerHTML = header() + ((tab === 'inbox' || tab === 'calls') ? callRows() : configPanel());
  }
  async function openCall(id) {
    selected = id;
    detail = await request('/calls/' + encodeURIComponent(id));
    render();
  }
  async function clicked(event) {
    const button = event.target.closest('[data-pa]');
    if (!button || loading) return;
    try {
      switch (button.dataset.pa) {
        case 'tab': tab = button.dataset.value; render(); break;
        case 'refresh': await refresh(); break;
        case 'sync': button.disabled = true; await request('/sync','POST'); await refresh(); flash('Calls synced'); break;
        case 'open': await openCall(button.dataset.value); break;
        case 'close-detail': selected = ''; detail = null; render(); break;
        case 'copy': await navigator.clipboard.writeText(button.dataset.value); flash('Number copied'); break;
        case 'filter': filters = {q:root.querySelector('#pa-search').value,kind:root.querySelector('#pa-kind').value,status:root.querySelector('#pa-filter-status').value}; render(); break;
        case 'test-notification': button.disabled = true; { const r = await request('/notification-test','POST'); flash(`Test alert: ${r.status}`, r.status === 'failed' || r.status === 'uncertain' ? 'error' : 'success'); await refresh(); } break;
        case 'check-delivery': button.disabled = true; await request('/notification-status','POST'); await refresh(); flash('Delivery status checked'); break;
      }
    } catch (err) { flash(err.message,'error'); button.disabled = false; }
  }
  async function submitted(event) {
    const form = event.target;
    if (!['pa-followup','pa-note','pa-config-form'].includes(form.id)) return;
    event.preventDefault();
    if (loading) return;
    loading = true;
    const clickedPublish = event.submitter?.name === 'publish';
    const values = Object.fromEntries(new FormData(form));
    form.querySelectorAll('button').forEach(b => b.disabled = true);
    try {
      if (form.id === 'pa-followup') {
        if (values.due_at) values.due_at = new Date(values.due_at).toISOString();
        await request('/calls/' + encodeURIComponent(selected),'PATCH',values);
        await refresh(); await openCall(selected); flash('Follow-up saved');
      } else if (form.id === 'pa-note') {
        await request('/calls/' + encodeURIComponent(selected) + '/notes','POST',values);
        await openCall(selected); flash('Note saved');
      } else {
        const config = {...data.config,...values}; delete config.publish;
        await request('/config','PUT',{config,revision:data.revision});
        if (clickedPublish) await request('/publish','POST');
        await refresh(); flash(clickedPublish ? 'Agent configuration activated' : 'Draft saved');
      }
    } catch (err) { flash(err.message,'error'); form.querySelectorAll('button').forEach(b => b.disabled = false); }
    finally { loading = false; }
  }
  window.PersonalAgent = {mount: async (element, apiHelper, toastHelper) => {
    root = element; api = apiHelper; toast = toastHelper;
    if (!mounted) { root.addEventListener('click',clicked); root.addEventListener('submit',submitted); mounted = true; }
    await refresh();
  }};
})();
