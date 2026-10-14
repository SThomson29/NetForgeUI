"""
Request parsing for the JSON API routes.

Routes used to read fields straight off request.json, so a missing body,
the wrong content type or a missing field raised inside the view and came
back as a 500 HTML page. The editor shows the server's error text in a
toast, so these answer with a 400 and a JSON reason instead.
"""

from flask import abort, jsonify, make_response, request


def bad_request(message):
    """Abort with a 400 carrying {ok: false, error: message}."""
    abort(make_response(jsonify(ok=False, error=message), 400))


def json_body(*required, kind=dict):
    """The request's JSON body, which must be a `kind` (dict or list).

    For a dict, every name in `required` must be present and not null.
    Empty strings are allowed: some fields are legitimately blank.
    """
    data = request.get_json(silent=True)
    if not isinstance(data, kind):
        bad_request('Expected a JSON %s.' % ('object' if kind is dict else 'list'))
    if kind is dict:
        missing = [k for k in required if data.get(k) is None]
        if missing:
            bad_request('Missing field%s: %s.' % (
                '' if len(missing) == 1 else 's', ', '.join(missing)))
    return data
