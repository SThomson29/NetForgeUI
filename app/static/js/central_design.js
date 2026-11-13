/*
 * Central design editor (React, no build step).
 *
 * Loaded by templates/central_design.html after React and an inline block
 * that defines CENTRAL_BASE, CAN_EDIT and START_SECTION.
 *
 * Nothing here knows what a design contains. The sections, their fields
 * and every profile type come from the Central collection through
 * CENTRAL_BASE/api/schema and /api/profile-type/<type>, so a section added
 * to the collection appears here once the collection is updated.
 *
 * Served as a plain file, so it never passes through Jinja.
 */
(function () {
  const h = React.createElement;
  const { useState, useEffect, useRef } = React;

  let MASK = '********';
  const profileCache = {};

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: 'application/json' } });
    const body = await res.json().catch(() => ({}));
    if (!res.ok || body.ok === false) throw new Error(body.error || ('Request failed (' + res.status + ')'));
    return body;
  }

  function isEmpty(v) {
    return v === undefined || v === null || v === '' ||
      (Array.isArray(v) && v.length === 0) ||
      (typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === 0);
  }

  function withDefault(field, value) {
    return value === undefined && field.default !== undefined ? field.default : value;
  }

  /* ── One field ───────────────────────────────────────────── */

  function Checks(props) {
    const chosen = Array.isArray(props.value) ? props.value.map(String) : [];
    if (!props.options.length) return h('div', { className: 'hint' }, props.empty || 'Nothing to choose from yet.');
    return h('div', { className: 'cd-checks' }, props.options.map(function (opt) {
      const on = chosen.includes(String(opt));
      return h('label', { key: opt },
        h('input', {
          type: 'checkbox', checked: on, disabled: props.disabled,
          onChange: function () {
            props.onChange(on ? chosen.filter(v => v !== String(opt)) : chosen.concat([String(opt)]));
          }
        }), String(opt));
    }));
  }

  function Control(props) {
    const f = props.field, disabled = !CAN_EDIT;
    const value = withDefault(f, props.value);
    const set = props.onChange;
    const type = f.type;

    if (type === 'toggle') {
      return h('label', { style: { display: 'inline-flex', gap: 8, alignItems: 'center', fontSize: 13, cursor: 'pointer' } },
        h('input', { type: 'checkbox', checked: value === true, disabled, onChange: e => set(e.target.checked) }),
        value === true ? 'On' : 'Off');
    }
    if (type === 'number') {
      return h('input', {
        className: 'form-input', type: 'number', disabled, min: f.min, max: f.max,
        value: value === undefined || value === null ? '' : value,
        onChange: function (e) { set(e.target.value === '' ? undefined : parseInt(e.target.value, 10)); }
      });
    }
    if (type === 'choice' && f.from === 'catalogue') {
      const groups = {};
      (props.ctx.profileTypes || []).forEach(function (p) { (groups[p.category] = groups[p.category] || []).push(p); });
      return h('select', { className: 'form-input', disabled, value: value || '', onChange: e => set(e.target.value || undefined) },
        h('option', { value: '' }, 'Choose a type…'),
        Object.keys(groups).sort().map(cat => h('optgroup', { key: cat, label: cat },
          groups[cat].map(p => h('option', { key: p.type, value: p.type }, p.label + ' (' + p.type + ')')))));
    }
    if (type === 'choice') {
      return h('select', { className: 'form-input', disabled, value: value === undefined ? '' : String(value), onChange: e => set(e.target.value || undefined) },
        h('option', { value: '' }, f.required ? 'Choose…' : '—'),
        (f.choices || []).map(c => h('option', { key: c, value: String(c) }, String(c))));
    }
    if (type === 'choices') {
      return h(Checks, { options: f.choices || [], value, disabled, onChange: v => set(v.length ? v : undefined) });
    }
    if (type === 'ref') {
      const options = (props.ctx.refs[f.to] || []);
      if (f.multiple) {
        return h(Checks, { options, value, disabled, empty: 'Nothing in the design to choose yet.', onChange: v => set(v.length ? v : undefined) });
      }
      return h('select', { className: 'form-input', disabled, value: value || '', onChange: e => set(e.target.value || undefined) },
        h('option', { value: '' }, '—'), options.map(o => h('option', { key: o, value: o }, o)));
    }
    if (type === 'secret') {
      const saved = value === MASK;
      return h('input', {
        className: 'form-input mono', type: 'password', disabled, autoComplete: 'new-password',
        placeholder: saved ? 'Saved — type to replace it' : '',
        value: saved ? '' : (value || ''),
        onChange: function (e) { set(e.target.value === '' ? (props.wasSaved ? MASK : undefined) : e.target.value); }
      });
    }
    return h('input', {
      className: 'form-input', type: 'text', disabled, maxLength: f.max_length,
      value: value === undefined || value === null ? '' : value,
      onChange: e => set(e.target.value === '' ? undefined : e.target.value)
    });
  }

  /* Lists are edited as text, one per line. The text is kept beside the
     value so a blank line being typed isn't swallowed mid-edit. */
  function ListControl(props) {
    const [text, setText] = useState(Array.isArray(props.value) ? props.value.join('\n') : '');
    const f = props.field;
    return h('textarea', {
      className: 'form-input mono', disabled: !CAN_EDIT, placeholder: 'One per line',
      rows: Math.max(2, Math.min(8, text.split('\n').length)), value: text,
      onChange: function (e) {
        setText(e.target.value);
        const out = e.target.value.split('\n').map(s => s.trim()).filter(s => s !== '')
          .map(s => f.of === 'number' && /^-?\d+$/.test(s) ? parseInt(s, 10) : s);
        props.onChange(out.length ? out : undefined);
      }
    });
  }

  function Field(props) {
    const f = props.field;
    const wide = f.type === 'choices' || f.type === 'list' || (f.type === 'ref' && f.multiple);
    const control = f.type === 'list'
      ? h(ListControl, { field: f, value: props.value, onChange: props.onChange })
      : h(Control, props);
    return h('div', { className: 'fld' + (wide ? ' cd-wide' : '') },
      h('label', { className: 'lbl' }, f.label || f.name, f.required ? h('span', { style: { color: '#dc2626' } }, ' *') : null,
        props.onRemove ? h('button', { className: 'cd-x', title: 'Remove this setting', type: 'button', onClick: props.onRemove }, '×') : null),
      control,
      f.help ? h('div', { className: 'hint' }, f.help) : null,
      f.devices && f.devices.length < 4 ? h('div', { className: 'hint' }, 'Applies to: ' + f.devices.join(', ')) : null);
  }

  /* ── A profile's settings ────────────────────────────────── */

  /* A profile type can have hundreds of settings, so only the ones that
     are set (or required) are shown, with a picker to add another. */
  function Settings(props) {
    const fields = props.fields || [];
    const value = props.value && typeof props.value === 'object' && !Array.isArray(props.value) ? props.value : {};
    const [added, setAdded] = useState([]);
    const shown = fields.filter(f => f.required || !isEmpty(value[f.name]) || added.includes(f.name));
    const hidden = fields.filter(f => !shown.includes(f));

    function change(name, v) {
      const next = Object.assign({}, value);
      if (v === undefined) delete next[name]; else next[name] = v;
      props.onChange(Object.keys(next).length ? next : undefined);
    }
    function remove(name) {
      setAdded(added.filter(n => n !== name));
      change(name, undefined);
    }

    const simple = shown.filter(f => f.type !== 'group' && f.type !== 'rows');
    const nested = shown.filter(f => f.type === 'group' || f.type === 'rows');

    return h('div', null,
      simple.length ? h('div', { className: 'cd-grid' }, simple.map(f => h(Field, {
        key: f.name, field: f, value: value[f.name], ctx: props.ctx, wasSaved: value[f.name] === MASK,
        onChange: v => change(f.name, v), onRemove: CAN_EDIT && !f.required ? () => remove(f.name) : null
      }))) : null,
      nested.map(function (f) {
        const head = h('div', { className: 't' }, f.label || f.name,
          CAN_EDIT && !f.required ? h('button', { className: 'cd-x', type: 'button', title: 'Remove', onClick: () => remove(f.name) }, '×') : null);
        if (f.type === 'group') {
          return h('div', { className: 'cd-group', key: f.name }, head,
            f.help ? h('div', { className: 'hint', style: { marginBottom: 6 } }, f.help) : null,
            h(Settings, { fields: f.fields, value: value[f.name], ctx: props.ctx, onChange: v => change(f.name, v) }));
        }
        const rows = Array.isArray(value[f.name]) ? value[f.name] : [];
        return h('div', { className: 'cd-group', key: f.name }, head,
          f.help ? h('div', { className: 'hint', style: { marginBottom: 6 } }, f.help) : null,
          rows.map((row, i) => h('div', { className: 'cd-row', key: i },
            h('div', { className: 'cd-row-head', style: { cursor: 'default' } },
              h('span', { className: 'grow' }, (f.key && row && row[f.key] !== undefined ? String(row[f.key]) : (f.label || f.name) + ' ' + (i + 1))),
              CAN_EDIT ? h('button', { className: 'cd-x', type: 'button', title: 'Remove', onClick: () => change(f.name, rows.filter((_, j) => j !== i)) }, '×') : null),
            h('div', { className: 'cd-row-body' },
              h(Settings, {
                fields: f.fields, value: row, ctx: props.ctx,
                onChange: v => change(f.name, rows.map((r, j) => j === i ? (v || {}) : r))
              })))),
          CAN_EDIT ? h('button', { className: 'btn btn-sm', type: 'button', onClick: () => change(f.name, rows.concat([{}])) }, '+ Add') : null);
      }),
      CAN_EDIT && hidden.length ? h('div', { className: 'fld', style: { maxWidth: 420, marginTop: 6 } },
        h('select', {
          className: 'form-input', value: '',
          onChange: function (e) { if (e.target.value) setAdded(added.concat([e.target.value])); }
        },
          h('option', { value: '' }, 'Add a setting… (' + hidden.length + ' more)'),
          hidden.map(f => h('option', { key: f.name, value: f.name }, (f.label || f.name) + '  ·  ' + f.name)))) : null);
  }

  function ProfileSettings(props) {
    const [profile, setProfile] = useState(profileCache[props.type] || null);
    const [error, setError] = useState('');
    useEffect(function () {
      if (!props.type) { setProfile(null); return; }
      if (profileCache[props.type]) { setProfile(profileCache[props.type]); return; }
      setProfile(null); setError('');
      getJSON(CENTRAL_BASE + '/api/profile-type/' + encodeURIComponent(props.type))
        .then(function (body) { profileCache[props.type] = body.profile; setProfile(body.profile); })
        .catch(e => setError(e.message));
    }, [props.type]);
    if (!props.type) return h('div', { className: 'hint cd-wide' }, 'Choose a type to see its settings.');
    if (error) return h('div', { className: 'hint cd-wide', style: { color: '#dc2626' } }, error);
    if (!profile) return h('div', { className: 'hint cd-wide' }, 'Loading settings…');
    return h('div', { className: 'cd-group cd-wide' },
      h('div', { className: 't' }, (profile.label || props.type) + ' settings'),
      profile.help ? h('div', { className: 'hint', style: { marginBottom: 6 } }, profile.help) : null,
      h(Settings, { fields: profile.fields, value: props.value, ctx: props.ctx, onChange: props.onChange }));
  }

  /* ── One row of a section ────────────────────────────────── */

  function hiddenBy(field, row, byName) {
    return Object.keys(field.show_if || {}).some(function (other) {
      const current = row[other] !== undefined ? row[other] : (byName[other] || {}).default;
      return current !== field.show_if[other];
    });
  }

  function Row(props) {
    const part = props.part, row = props.row;
    const byName = {};
    part.fields.forEach(f => { byName[f.name] = f; });
    const title = part.kind === 'profiles'
      ? [row.profile_type, row.name].filter(Boolean).join(' · ') || 'New profile'
      : (part.key && row[part.key] !== undefined && row[part.key] !== '' ? String(row[part.key]) : 'New ' + (part.label || 'row').toLowerCase().replace(/s$/, ''));

    function change(name, v) {
      const next = Object.assign({}, row);
      if (v === undefined) delete next[name]; else next[name] = v;
      if (name === 'profile_type') delete next.config;
      props.onChange(next);
    }

    return h('div', { className: 'cd-row' },
      h('div', { className: 'cd-row-head', onClick: props.onToggle },
        h('span', null, props.open ? '▾' : '▸'),
        h('span', { className: 'grow' }, title),
        CAN_EDIT ? h('button', {
          className: 'cd-x', type: 'button', title: 'Remove',
          onClick: function (e) { e.stopPropagation(); if (confirm('Remove ' + title + ' from the design?')) props.onRemove(); }
        }, '×') : null),
      props.open ? h('div', { className: 'cd-row-body' },
        h('div', { className: 'cd-grid' }, part.fields.filter(f => !hiddenBy(f, row, byName)).map(function (f) {
          if (f.type === 'profile') {
            return h(ProfileSettings, { key: f.name, type: row.profile_type, value: row.config, ctx: props.ctx, onChange: v => change('config', v) });
          }
          return h(Field, {
            key: f.name, field: f, value: row[f.name], ctx: props.ctx,
            wasSaved: props.saved && props.saved[f.name] === MASK, onChange: v => change(f.name, v)
          });
        }))) : null);
  }

  /* ── A section ───────────────────────────────────────────── */

  function Section(props) {
    const section = props.section;
    const [state, setState] = useState(null);       // {data, base, yaml, refs}
    const [saved, setSaved] = useState(null);       // data as last loaded or saved
    const [open, setOpen] = useState({});
    const [problems, setProblems] = useState([]);
    const [error, setError] = useState('');
    const [busy, setBusy] = useState(false);
    const [showYaml, setShowYaml] = useState(false);
    const dirty = useRef(false);

    useEffect(function () {
      setState(null); setProblems([]); setError(''); setOpen({}); dirty.current = false; window.__centralDirty = false;
      getJSON(CENTRAL_BASE + '/api/design/' + section.id)
        .then(function (body) { setState(body); setSaved(JSON.parse(JSON.stringify(body.data))); })
        .catch(e => setError(e.message));
    }, [section.id]);

    if (error && !state) return h('div', { className: 'card' }, h('p', { style: { color: '#dc2626', fontSize: 13, margin: 0 } }, error));
    if (!state) return h('div', { className: 'card' }, h('div', { className: 'empty' }, h('p', null, 'Loading…')));

    function setRows(v, rows) {
      dirty.current = true; window.__centralDirty = true;
      setState(Object.assign({}, state, { data: Object.assign({}, state.data, { [v]: rows }) }));
    }

    async function save() {
      setBusy(true); setProblems([]); setError('');
      try {
        const res = await fetch(CENTRAL_BASE + '/api/design/' + section.id + '/save', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ data: state.data, base: state.base })
        });
        const body = await res.json().catch(() => ({}));
        if (!res.ok || !body.ok) {
          setError(body.error || 'The section was not saved.');
          setProblems(body.problems || []);
          return;
        }
        dirty.current = false; window.__centralDirty = false;
        setState(Object.assign({}, state, { data: body.data, base: body.base, yaml: body.yaml }));
        setSaved(JSON.parse(JSON.stringify(body.data)));
        props.onSaved(section.id, body.data);
        toast('Saved ' + section.label);
      } catch (e) {
        setError(e.message);
      } finally {
        setBusy(false);
      }
    }

    const ctx = { refs: Object.assign({}, state.refs), profileTypes: props.profileTypes };
    // Rows being edited can be picked by references in the same section.
    section.lists.forEach(function (part) {
      if (part.key) ctx.refs[part.var] = (state.data[part.var] || []).map(r => r[part.key]).filter(v => v !== undefined && v !== '').map(String);
    });

    return h('div', null,
      h('div', { className: 'card' },
        h('div', { className: 'card-header' }, section.label),
        section.help ? h('p', { style: { fontSize: 13, lineHeight: 1.6, margin: '0 0 4px', color: 'var(--text-secondary)' } }, section.help) : null,
        section.needs && section.needs.length ? h('div', { className: 'hint' }, 'Applied after: ' + section.needs.join(', ').replace(/_/g, ' ')) : null),

      error ? h('div', { className: 'cd-problems' }, error,
        problems.length ? h('ul', null, problems.map((p, i) => h('li', { key: i }, p))) : null) : null,

      section.lists.map(function (part) {
        const rows = state.data[part.var] || [];
        const before = (saved && saved[part.var]) || [];
        return h('div', { className: 'card', key: part.var },
          section.lists.length > 1 ? h('div', { className: 'card-header' }, part.label) : null,
          rows.length === 0 ? h('div', { className: 'hint', style: { marginBottom: 10 } }, 'Nothing here yet.') : null,
          rows.map((row, i) => h(Row, {
            key: i, part, row, ctx, saved: before[i],
            open: open[part.var + i] !== false && (open[part.var + i] === true || rows.length <= 3),
            onToggle: () => setOpen(Object.assign({}, open, { [part.var + i]: !(open[part.var + i] !== false && (open[part.var + i] === true || rows.length <= 3)) })),
            onChange: next => setRows(part.var, rows.map((r, j) => j === i ? next : r)),
            onRemove: () => setRows(part.var, rows.filter((_, j) => j !== i))
          })),
          CAN_EDIT ? h('button', {
            className: 'btn btn-sm', type: 'button',
            onClick: function () { setRows(part.var, rows.concat([{}])); setOpen(Object.assign({}, open, { [part.var + rows.length]: true })); }
          }, '+ Add') : null);
      }),

      h('div', { className: 'cd-bar' },
        CAN_EDIT ? h('button', { className: 'btn btn-primary', disabled: busy, onClick: save }, busy ? 'Saving…' : 'Save ' + section.label.toLowerCase()) : null,
        h('button', { className: 'btn', type: 'button', onClick: () => setShowYaml(!showYaml) }, showYaml ? 'Hide YAML' : 'Show as YAML'),
        !CAN_EDIT ? h('span', { className: 'hint' }, 'You have view access; the design can\'t be changed.') : null),

      showYaml ? h('div', { style: { marginTop: 12 } },
        h('div', { className: 'hint', style: { marginBottom: 6 } },
          'This section as it was last saved, the way the playbook ' + (section.playbook || '') + ' reads it. Secrets are hidden.'),
        h('pre', { className: 'cd-yaml' }, state.yaml || '# Nothing saved yet')) : null);
  }

  /* ── The page ────────────────────────────────────────────── */

  function App() {
    const [meta, setMeta] = useState(null);
    const [error, setError] = useState('');
    const [current, setCurrent] = useState(START_SECTION);
    const [counts, setCounts] = useState({});

    useEffect(function () {
      getJSON(CENTRAL_BASE + '/api/schema').then(function (body) {
        MASK = body.mask || MASK;
        setMeta(body); setCounts(body.counts || {});
        const ids = body.schema.sections.map(s => s.id);
        setCurrent(c => ids.includes(c) ? c : ids[0]);
      }).catch(e => setError(e.message));
    }, []);

    if (error) return h('div', { className: 'card' }, h('p', { style: { color: '#dc2626', fontSize: 13, margin: 0 } }, error));
    if (!meta) return h('div', { className: 'card' }, h('div', { className: 'empty' }, h('p', null, 'Loading the design…')));

    const sections = meta.schema.sections;
    const section = sections.find(s => s.id === current) || sections[0];

    function pick(id) {
      if (window.__centralDirty && !confirm('You have unsaved changes in this section. Leave without saving?')) return;
      window.__centralDirty = false;
      setCurrent(id);
      history.replaceState(null, '', CENTRAL_BASE + '/design?section=' + encodeURIComponent(id));
    }

    return h('div', { className: 'cd-wrap' },
      h('div', { className: 'cd-nav' }, meta.stages.map(function (stage) {
        const mine = sections.filter(s => s.stage === stage[0]);
        if (!mine.length) return null;
        return h('div', { key: stage[0] },
          h('div', { className: 'cd-stage' }, stage[1]),
          mine.map(s => h('button', { key: s.id, type: 'button', className: 'cd-item' + (s.id === section.id ? ' on' : ''), onClick: () => pick(s.id) },
            h('span', null, s.label), h('span', { className: 'n' }, counts[s.id] ? counts[s.id] : ''))));
      })),
      h(Section, {
        key: section.id, section, profileTypes: meta.profile_types,
        onSaved: function (id, data) {
          const n = Object.keys(data).reduce((sum, k) => sum + (data[k] || []).length, 0);
          setCounts(c => Object.assign({}, c, { [id]: n }));
        }
      }));
  }

  window.addEventListener('beforeunload', function (e) {
    if (window.__centralDirty) { e.preventDefault(); e.returnValue = ''; }
  });

  ReactDOM.createRoot(document.getElementById('design-root')).render(h(App));
})();
