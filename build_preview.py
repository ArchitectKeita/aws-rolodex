"""Bakes the pretend backend into a copy of the page, for showing the app without AWS."""
import json, pathlib, re
here = pathlib.Path(__file__).parent
page = (here.parent / "app" / "static" / "index.html").read_text(encoding="utf-8")
mock = (here / "mock.js").read_text(encoding="utf-8").replace("/*__SEED__*/[]", (here / "seed.json").read_text())
page = page.replace("<script>/*__MOCK__*/</script>", "<script>" + mock + "</script>")
page = page.replace("<p>Anyone can look around.", "<p><b style='color:#ece6dc'>Preview:</b> the key is <code>preview</code>. Anyone can look around.")
(here / "preview.html").write_text(page, encoding="utf-8")
# artifact copy: the host supplies the document skeleton
body = re.sub(r'<!doctype html>\s*|<html[^>]*>\s*|</html>\s*|<head>\s*|</head>\s*|</body>\s*|<meta charset="utf-8">\s*|<meta name="viewport"[^>]*>\s*', "", page, flags=re.I)
body = body.replace('<body data-view="home">', '<script>document.addEventListener("DOMContentLoaded",()=>{if(!document.body.dataset.view)document.body.dataset.view="home"})</script>')
(here / "artifact.html").write_text(body, encoding="utf-8")
print("ok", len(page) // 1024, "KB")
