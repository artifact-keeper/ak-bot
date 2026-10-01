#!/usr/bin/env python3
"""Create the ak-bot GitHub App through the manifest flow and store its
credentials as secrets on the ak-bot repo, using only `gh` and a browser.

    python3 scripts/create-github-app.py [--org artifact-keeper] [--repo artifact-keeper/ak-bot]

What happens:
  1. a local page auto-submits the App manifest to GitHub;
  2. you click "Create GitHub App" once;
  3. GitHub redirects to http://localhost:PORT/callback?code=...;
  4. this script exchanges the code (POST /app-manifests/{code}/conversions),
     writes the private key to ~/.config/ak-bot/<slug>.pem (mode 600), and sets
     AK_BOT_APP_ID and AK_BOT_APP_PRIVATE_KEY on the repo with `gh secret set`.
Installing the App on repositories is a second, separate click; the script
prints that URL at the end.
"""
import argparse, http.server, json, os, pathlib, subprocess, sys, tempfile, threading, urllib.parse, webbrowser

ap = argparse.ArgumentParser()
ap.add_argument("--org", default="artifact-keeper")
ap.add_argument("--repo", default="artifact-keeper/ak-bot")
ap.add_argument("--name", default="ak-bot")
ap.add_argument("--port", type=int, default=8765)
a = ap.parse_args()

manifest = {
    "name": a.name,
    "url": f"https://github.com/{a.repo}",
    "description": "JEV-powered decision bots for the artifact-keeper org",
    "public": False,
    "redirect_url": f"http://localhost:{a.port}/callback",
    "hook_attributes": {"url": f"https://github.com/{a.repo}", "active": False},
    "default_permissions": {
        "contents": "write", "issues": "write", "pull_requests": "write",
        "actions": "write", "metadata": "read",
    },
    "default_events": [],
}
state = os.urandom(8).hex()
result = {}

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path != "/callback" or q.get("state", [""])[0] != state or "code" not in q:
            self.send_response(404); self.end_headers(); return
        result["code"] = q["code"][0]
        self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
        self.wfile.write(b"<h2>ak-bot: got the code. You can close this tab; the terminal finishes the setup.</h2>")
        threading.Thread(target=self.server.shutdown, daemon=True).start()

srv = http.server.HTTPServer(("127.0.0.1", a.port), H)
page = pathlib.Path(tempfile.gettempdir()) / "ak-bot-app-manifest.html"
page.write_text(f"""<!doctype html><meta charset=utf-8><title>ak-bot App manifest</title>
<body onload="document.forms[0].submit()">
<form method="post" action="https://github.com/organizations/{a.org}/settings/apps/new?state={state}">
<input type="hidden" name="manifest" value='{json.dumps(manifest).replace("'", "&#39;")}'>
<noscript><button>Create the ak-bot GitHub App</button></noscript>
</form><p>Submitting the manifest to GitHub…</p></body>""")
print(f"Opening {page} in your browser; click 'Create GitHub App' when the form appears.")
print(f"If no browser opens, open that file by hand. Waiting on http://localhost:{a.port}/callback ...")
webbrowser.open(page.as_uri())
srv.serve_forever()

code = result.get("code")
if not code:
    sys.exit("no code received")
out = subprocess.run(["gh", "api", "-X", "POST", f"/app-manifests/{code}/conversions"], capture_output=True, text=True)
if out.returncode != 0:
    sys.exit(f"conversion failed: {out.stderr}")
app = json.loads(out.stdout)
app_id, slug, pem = app["id"], app["slug"], app["pem"]
keydir = pathlib.Path.home() / ".config" / "ak-bot"
keydir.mkdir(parents=True, exist_ok=True)
keyfile = keydir / f"{slug}.pem"
keyfile.write_text(pem); keyfile.chmod(0o600)
for name, value in (("AK_BOT_APP_ID", str(app_id)), ("AK_BOT_APP_PRIVATE_KEY", pem)):
    r = subprocess.run(["gh", "secret", "set", name, "-R", a.repo], input=value, capture_output=True, text=True)
    print(f"{name}: {'set' if r.returncode == 0 else r.stderr.strip()}")
print(f"\nApp '{slug}' created, id {app_id}. Private key saved to {keyfile} (do not commit it).")
print(f"Install it (one more click): https://github.com/apps/{slug}/installations/new")
print(f"Set the logo under: https://github.com/organizations/{a.org}/settings/apps/{slug}")
