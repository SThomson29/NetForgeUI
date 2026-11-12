/*
 * Central Apply page: unlock the tenant, plan, apply.
 *
 * Loaded by templates/central_apply.html after an inline block that
 * defines CENTRAL_BASE and TENANT. Served as a plain file, so it never
 * passes through Jinja.
 *
 * Everything written into the page that came from the tenant or the
 * playbook goes through textContent, never innerHTML.
 */
(function () {
  const $ = id => document.getElementById(id);
  let polling = null;
  let plannedFor = null;          // the sections of the last plan that finished cleanly

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'text') node.textContent = attrs[k];
      else if (k === 'className') node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(c => node.appendChild(c));
    return node;
  }

  async function post(path, body) {
    const res = await fetch(CENTRAL_BASE + path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok && data.ok !== false, data };
  }

  /* ── Tenant ──────────────────────────────────────────────── */

  function showTenant(tenant) {
    $('tenant-unlocked').style.display = tenant.unlocked ? 'block' : 'none';
    $('tenant-locked').style.display = tenant.unlocked ? 'none' : 'block';
    if (tenant.unlocked) {
      $('tenant-url').textContent = tenant.base_url;
      $('tenant-client').textContent = tenant.client_id;
      $('tenant-idle').textContent = tenant.idle_minutes;
    }
    $('plan-btn').disabled = !tenant.unlocked;
    if (!tenant.unlocked) setApply(null);
  }

  window.unlockTenant = async function () {
    $('unlock-error').textContent = '';
    $('unlock-btn').disabled = true;
    $('unlock-btn').textContent = 'Checking…';
    const r = await post('/unlock', {
      base_url: $('t-url').value, client_id: $('t-id').value, client_secret: $('t-secret').value
    });
    $('unlock-btn').disabled = false;
    $('unlock-btn').textContent = 'Unlock';
    if (!r.ok) { $('unlock-error').textContent = r.data.error || 'Could not unlock.'; return; }
    $('t-secret').value = '';
    showTenant(r.data.tenant);
  };

  window.lockTenant = async function () {
    const r = await post('/lock');
    if (r.ok) showTenant(r.data.tenant);
  };

  /* ── Runs ────────────────────────────────────────────────── */

  function chosen() {
    return Array.from(document.querySelectorAll('.ca-section:checked')).map(b => b.value);
  }

  function setApply(sections) {
    plannedFor = sections;
    $('apply-btn').disabled = !sections;
  }

  function fail(message, problems) {
    const box = $('run-error');
    box.textContent = message || '';
    if (problems && problems.length) {
      box.appendChild(el('ul', { style: 'margin:6px 0 0 18px;padding:0' }, problems.map(p => el('li', { text: p }))));
    }
    box.style.display = message ? 'block' : 'none';
  }

  function diffBlock(diff) {
    const pre = el('pre', { className: 'ca-diff' });
    const show = v => typeof v === 'string' ? v : JSON.stringify(v, null, 2);
    if (diff.before !== undefined && Object.keys(diff.before || {}).length) {
      pre.appendChild(el('span', { className: 'b', text: '- ' + show(diff.before).split('\n').join('\n- ') + '\n' }));
    }
    if (diff.after !== undefined) {
      pre.appendChild(el('span', { className: 'a', text: '+ ' + show(diff.after).split('\n').join('\n+ ') }));
    }
    return pre;
  }

  function outcome(entry, mode) {
    if (entry.failed) return ['failed', 'badge-red'];
    if (entry.waiting) return ['waiting', 'badge-yellow'];
    if (entry.skipped) return ['skipped', 'badge-neutral'];
    if (entry.changed) return [mode === 'plan' ? 'would change' : 'changed', mode === 'plan' ? 'badge-yellow' : 'badge-green'];
    if (entry.step === 'Not assigned yet') return ['waiting', 'badge-yellow'];
    return ['no change', 'badge-neutral'];
  }

  function renderReports(job) {
    const box = $('run-reports');
    box.textContent = '';
    const labels = {};
    document.querySelectorAll('.ca-section').forEach(b => { labels[b.value] = b.parentNode.childNodes[1].textContent.trim(); });
    (job.results || []).forEach(function (report) {
      const entries = report.entries || [];
      const changed = entries.filter(e => e.changed).length;
      const failed = entries.filter(e => e.failed).length;
      const summary = el('summary', {}, [
        el('span', { text: labels[report.section] || report.section, style: 'flex:1' }),
        el('span', {
          className: 'badge ' + (failed ? 'badge-red' : changed ? (job.mode === 'plan' ? 'badge-yellow' : 'badge-green') : 'badge-neutral'),
          text: failed ? failed + ' failed' : changed ? changed + (job.mode === 'plan' ? ' would change' : ' changed') : 'no change'
        })
      ]);
      const rows = entries.map(function (entry) {
        const o = outcome(entry, job.mode);
        const detail = el('td', {});
        if (entry.message) detail.appendChild(el('div', { text: entry.message }));
        if (entry.diff) detail.appendChild(diffBlock(entry.diff));
        return el('tr', {}, [
          el('td', { text: entry.step }), el('td', { text: entry.item || '' }),
          el('td', {}, [el('span', { className: 'badge ' + o[1], text: o[0] })]), detail
        ]);
      });
      const table = el('table', {}, [el('tbody', {}, rows.length ? rows :
        [el('tr', {}, [el('td', { text: 'Nothing to do.', colspan: '4', style: 'color:var(--text-tertiary)' })])])]);
      const details = el('details', { className: 'ca-report' }, [summary, table]);
      if (failed || changed) details.setAttribute('open', '');
      box.appendChild(details);
    });
  }

  function running(on) {
    $('plan-btn').disabled = on;
    if (on) $('apply-btn').disabled = true;
    // Sections with nothing in them stay disabled either way.
    document.querySelectorAll('.ca-section').forEach(b => { b.disabled = on || b.dataset.none === '1'; });
  }

  async function start(mode) {
    const sections = chosen();
    fail('');
    if (!sections.length) { fail('Choose at least one section that has something in it.'); return; }
    running(true);
    $('run-status').textContent = mode === 'plan' ? 'Planning…' : 'Applying…';
    const r = await post('/run', { mode, sections });
    if (!r.ok) {
      running(false);
      $('run-status').textContent = '';
      fail(r.data.error || 'The run could not start.', r.data.problems);
      if (r.data.locked) showTenant({ unlocked: false });
      return;
    }
    $('run-card').style.display = 'block';
    $('run-title').textContent = mode === 'plan' ? 'Plan' : 'Apply';
    $('run-reports').textContent = '';
    $('ca-output').textContent = '';
    poll(r.data.job_id, mode, sections);
  }

  function poll(jobId, mode, sections) {
    clearInterval(polling);
    polling = setInterval(async function () {
      let job;
      try {
        const res = await fetch(CENTRAL_BASE + '/run/' + jobId);
        if (!res.ok) throw new Error('lost');
        job = await res.json();
      } catch (e) {
        clearInterval(polling); running(false);
        $('run-status').textContent = 'Lost track of the run. Its result is in History.';
        return;
      }
      $('ca-output').textContent = job.output || '';
      if (job.status !== 'done' && job.status !== 'failed') return;
      clearInterval(polling);
      running(false);
      renderReports(job);
      const failed = job.status === 'failed' || (job.results || []).some(r => r.failed);
      const changes = (job.results || []).reduce((n, r) => n + (r.entries || []).filter(e => e.changed).length, 0);
      if (failed) {
        $('run-status').textContent = (mode === 'plan' ? 'The plan' : 'The apply') + ' failed. See below.';
        setApply(null);
      } else if (mode === 'plan') {
        $('run-status').textContent = changes ? changes + ' change' + (changes === 1 ? '' : 's') + ' to make. Review them, then apply.'
          : 'The tenant already matches the design.';
        setApply(changes ? sections : null);
      } else {
        $('run-status').textContent = 'Applied. ' + changes + ' change' + (changes === 1 ? '' : 's') + ' made.';
        setApply(null);
      }
    }, 1000);
  }

  window.runPlan = function () { start('plan'); };

  window.runApply = function () {
    const now = chosen();
    if (!plannedFor || now.join() !== plannedFor.join()) {
      setApply(null);
      fail('The sections chosen have changed since the plan. Run the plan again.');
      return;
    }
    if (!confirm('Apply these changes to the tenant at ' + $('tenant-url').textContent + '?')) return;
    start('apply');
  };

  document.addEventListener('change', function (e) {
    if (e.target.classList && e.target.classList.contains('ca-section')) setApply(null);
  });
  document.querySelectorAll('.ca-section[disabled]').forEach(b => { b.dataset.none = '1'; });

  showTenant(TENANT);
})();
