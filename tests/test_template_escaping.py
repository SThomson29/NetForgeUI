"""
Values passed to event handlers.

Handlers were once inline (onclick="removePool('{{ name }}')"). Jinja
escapes a quote to &#39;, but the browser decodes the attribute before
handing it to JavaScript, so a name like x');alert(1);// broke out of the
string. Pool names are free text and travel between users via project
export and import, so this was reachable across accounts.

Inline handlers are now gone (the CSP blocks them): arguments travel as a
JSON array in data-args, written with |tojson inside a single-quoted
attribute. The check here is behavioural: after the browser's HTML
decoding, data-args must parse as the exact values that went in.
"""

import html
import json
import re

import pytest

HOSTILE = [
    "x');alert(document.cookie);//",
    'x");alert(1);//',
    "</button><script>alert(1)</script>",
    "it's & <fine> \"quoted\"",
]


def _handler_args(body, fn):
    """Decode the data-args of every element that calls fn, as the browser
    would see them."""
    found = []
    for m in re.finditer(r"""data-(?:click|change|input)="%s"\s+data-args=(['"])(.*?)\1"""
                         % re.escape(fn), body):
        found.append(json.loads(html.unescape(m.group(2))))
    return found


@pytest.mark.parametrize('name', HOSTILE)
def test_pool_name_stays_inside_a_string(app, auth_client, name):
    from app.project import create_project, add_pool
    with app.app_context():
        create_project(app, 'admin', 'xss')
        add_pool(app, 'admin', 'xss', {
            'id': 'pool_1', 'type': 'unique', 'name': name,
            'prefix': '32', 'subnet': '10.255.0.0/24'})
    body = auth_client.get('/projects/admin/xss/resources').data.decode()

    calls = _handler_args(body, 'removePool')
    assert calls, 'removePool handler not found'
    # Parsing as JSON proves the name could not have escaped its string.
    assert calls[0] == ['pool_1', name]

    edits = _handler_args(body, 'editPool')
    assert edits and edits[0][1] == name


def test_no_inline_handlers():
    """The CSP blocks them, so one coming back would silently do nothing."""
    import glob
    offenders = []
    for path in glob.glob('app/templates/*.html'):
        with open(path) as f:
            lines = f.readlines()
        for i, line in enumerate(lines, 1):
            if re.search(r'''\son[a-z]+\s*=\s*["']''', line) or 'javascript:' in line:
                offenders.append('%s:%d' % (path, i))
    assert not offenders, offenders


def test_data_args_from_jinja_use_tojson_in_single_quotes():
    """tojson escapes ' but not ", so the attribute must be single-quoted."""
    import glob
    offenders = []
    for path in glob.glob('app/templates/*.html'):
        with open(path) as f:
            for i, line in enumerate(f, 1):
                for m in re.finditer(r'''data-args=(["'])(.*?)\1''', line):
                    if '{{' in m.group(2) and not (m.group(1) == "'" and '|tojson' in m.group(2)):
                        offenders.append('%s:%d' % (path, i))
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# HTML built in JavaScript
#
# innerHTML and template literals bypass Jinja's autoescaping entirely. Pool
# names, host names, error text and playbook output — which can carry switch
# running-config content, and a version string read back from the switch —
# all went in raw.
# ---------------------------------------------------------------------------

# Interpolations that are deliberately HTML built by the page itself, or
# constant ternaries. Everything else must go through esc().
_INTENTIONAL_HTML = re.compile(
    r"^(esc\(|escapeHtml\(|encodeURIComponent\(|'\{\{|statusBadge$|diffHtml$"
    r"|lines$|summaryClass$|summaryMsg$|statusIcon$|r\.changed \?|failed > 0"
    r"|good \?|r\.uploaded \?|r\.booted \?|h\.mgmt_ip \? ''"
    r"|r\.previous_version \?|r\.unsafe_updates_allowed"
    # Built server-side from validated owner and project names, and the
    # sidebar's shared-by note, which escapes its own text.
    r"|PROJECT_BASE$|projectShared$)")


def test_html_built_in_javascript_escapes_its_data():
    import glob
    offenders = []
    for path in glob.glob('app/templates/*.html'):
        with open(path) as f:
            src = f.read()
        for lit in re.finditer(r'`([^`]*)`', src):
            body = lit.group(1)
            if not re.search(r'<[a-zA-Z/]', body):
                continue
            for m in re.finditer(r'\$\{([^}]*)\}', body):
                expr = m.group(1).strip()
                if not _INTENTIONAL_HTML.match(expr):
                    offenders.append('%s: ${%s}' % (path, expr))
    assert not offenders, offenders


def test_esc_covers_text_and_attribute_contexts():
    """Text needs & < >; quoted attributes also need both quote styles."""
    with open('app/templates/base.html') as f:
        src = f.read()
    fn = re.search(r'function esc\(value\) \{.*?\n\}', src, re.S).group(0)
    for char, entity in (('&', '&amp;'), ('<', '&lt;'), ('>', '&gt;'),
                         ('"', '&quot;'), ("'", '&#39;')):
        assert entity in fn, 'esc() does not escape %s' % char
    # & must be replaced first, or the other entities get double-escaped
    assert fn.index('&amp;') < fn.index('&lt;')


def test_esc_is_available_on_every_page():
    """Defined in base.html, which every other template extends.

    Partials (_name.html) are only ever included, so the pages that
    include them must extend base instead.
    """
    import glob
    import os
    sources = {}
    for path in glob.glob('app/templates/*.html'):
        with open(path) as f:
            sources[path] = f.read()

    def extends_base(src):
        return '{% extends "base' in src or "{% extends 'base" in src

    for path, src in sources.items():
        if 'esc(' not in src or path == 'app/templates/base.html':
            continue
        name = os.path.basename(path)
        if name.startswith('_'):
            includers = [p for p, s in sources.items() if name in s and p != path]
            assert includers, '%s is never included' % path
            for inc in includers:
                assert extends_base(sources[inc]), (path, inc)
        else:
            assert extends_base(src), path


# ---------------------------------------------------------------------------
# HTML built in JavaScript from server data
# ---------------------------------------------------------------------------
#
# The import preview renders manifest.json from an uploaded archive, so a
# crafted export could run script in whoever previews it. Lines that build
# markup from that data must pass every value through esc().

def _markup_lines(body, needle):
    """Lines that concatenate into an html string and mention needle."""
    return [line for line in body.splitlines()
            if needle in line and re.search(r"html\s*\+?=|innerHTML\s*=", line)]


def test_import_preview_escapes_manifest_values(auth_client):
    body = auth_client.get('/projects').data.decode()
    lines = _markup_lines(body, 'm.') + _markup_lines(body, 'hosts')
    assert lines, 'import preview markup not found'
    for line in lines:
        for ref in re.findall(r"\bm\.(source_project|exported)\b", line):
            assert re.search(r"esc\(m\.%s\b" % ref, line), line
        assert 'm.hosts.join' not in line, line
    assert any('hosts.map(esc)' in line for line in lines)


def test_reconcile_output_escapes_host_names(app, auth_client):
    from app.project import create_project
    with app.app_context():
        create_project(app, 'admin', 'recon')
    body = auth_client.get('/projects/admin/recon/hosts').data.decode()
    lines = _markup_lines(body, 'host')
    assert any("esc(host)" in line for line in lines)
    assert any("data.hosts[host].map(esc)" in line for line in lines)
    assert any("esc(data.error" in line for line in body.splitlines())
