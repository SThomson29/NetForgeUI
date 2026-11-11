# Monaco Editor 0.57.0

The editor component of VS Code, used by the project Files page. MIT
licensed; see LICENSE and ThirdPartyNotices.txt.

From the `monaco-editor@0.57.0` npm package: the files under `min/vs` that
the editor requests when it loads with the YAML language, and nothing else
(the full `min` folder is 25 MB; this is about 5 MB). `codicon.ttf` is the
icon font from `esm/vs/base/browser/ui/codicons/codicon/`.

One file differs from the published build: `vs/editor/editor.main.css`.
Its `@font-face` embedded the icon font as a `data:` URI, which the app's
Content-Security-Policy (`font-src`) blocks. It now points at
`../../codicon.ttf` instead. Everything else is byte for byte as published;
tests/test_files.py checks the hashes.

The folder name carries the version because the app caches these files in
the browser for a long time. A new version goes in a new folder.
