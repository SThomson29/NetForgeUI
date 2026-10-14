"""
Values interpolated into onclick/onsubmit handlers.

Jinja escapes a quote to &#39;, but the browser decodes the attribute before
handing it to JavaScript, so a name like x');alert(1);// broke out of the
string. Pool names are free text and travel between users via project
export and import, so this was reachable across accounts.

The handlers now use |tojson inside single-quoted attributes. The check
here is behavioural: after the browser's HTML decoding, the handler's
arguments must parse as a plain JSON array — which is only possible if the
hostile name stayed inside a string literal.
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
    """Decode every fn(...) handler the browser would see; return its args."""
    found = []
    for m in re.finditer(r"""on[a-z]+=(['"])(.*?)\1""", body):
        js = html.unescape(m.group(2))
        call = re.match(r'^\s*%s\((.*)\)\s*$' % re.escape(fn), js)
        if call:
            found.append(json.loads('[' + call.group(1) + ']'))
    return found


@pytest.mark.parametrize('name', HOSTILE)
def test_pool_name_stays_inside_a_string(app, auth_client, name):
    from app.project import create_project, add_pool
    with app.app_context():
        create_project(app, 'admin', 'xss')
        add_pool(app, 'admin', 'xss', {
            'id': 'pool_1', 'type': 'unique', 'name': name,
            'prefix': '32', 'subnet': '10.255.0.0/24'})
    body = auth_client.get('/projects/xss/resources').data.decode()

    calls = _handler_args(body, 'removePool')
    assert calls, 'removePool handler not found'
    # Parsing as JSON proves the name could not have escaped its string.
    assert calls[0] == ['pool_1', name]

    edits = _handler_args(body, 'editPool')
    assert edits and edits[0][1] == name


def test_no_handler_interpolates_inside_a_quoted_string():
    """Guard against the old pattern coming back in any template."""
    import glob
    offenders = []
    for path in glob.glob('app/templates/*.html'):
        for i, line in enumerate(open(path), 1):
            if re.search(r'''on[a-z]+=["'][^"']*'\{\{''', line):
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
    r"|r\.previous_version \?|r\.unsafe_updates_allowed)")


def test_html_built_in_javascript_escapes_its_data():
    import glob
    offenders = []
    for path in glob.glob('app/templates/*.html'):
        src = open(path).read()
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
    src = open('app/templates/base.html').read()
    fn = re.search(r'function esc\(value\) \{.*?\n\}', src, re.S).group(0)
    for char, entity in (('&', '&amp;'), ('<', '&lt;'), ('>', '&gt;'),
                         ('"', '&quot;'), ("'", '&#39;')):
        assert entity in fn, 'esc() does not escape %s' % char
    # & must be replaced first, or the other entities get double-escaped
    assert fn.index('&amp;') < fn.index('&lt;')


def test_esc_is_available_on_every_page():
    """Defined in base.html, which every other template extends."""
    import glob
    for path in glob.glob('app/templates/*.html'):
        src = open(path).read()
        if 'esc(' in src and path != 'app/templates/base.html':
            assert ('{% extends "base' in src or "{% extends 'base" in src), path


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
    body = auth_client.get('/projects/recon/hosts').data.decode()
    lines = _markup_lines(body, 'host')
    assert any("esc(host)" in line for line in lines)
    assert any("data.hosts[host].map(esc)" in line for line in lines)
    assert any("esc(data.error" in line for line in body.splitlines())
