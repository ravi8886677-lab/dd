# Dashboard frontend

The dashboard is a local-only browser interface for Jarvis memories, chat,
settings and connections.

## Assets

- Browser markup lives in `templates/index.html`.
- Styling lives in `static/dashboard.css` and uses the shared dashboard theme
  variables as its single palette.
- Browser behaviour lives in `static/dashboard.js` so JavaScript tooling can
  parse it directly.
- The page loads no remote scripts, styles, fonts, preconnect hints or other
  resources. All frontend requests resolve through the local Flask server.

## Runtime and packaging

The Flask application serves templates and static files from the source tree
in development and from `sys._MEIPASS` in a frozen PyInstaller application.
Both `desktop_app.memory_viewer:app` and
`python -m desktop_app.memory_viewer` remain supported entry points.

## Access control

Every request passes through the server-wide Host, session-token and rate-limit
guard before a route or static asset is served. The successful page response
stores the per-launch token in a host-only, same-site cookie for subsequent
asset and API requests.
