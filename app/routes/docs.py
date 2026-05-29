import os
import re
import markdown
from flask import Blueprint, render_template, abort
from werkzeug.utils import safe_join

bp = Blueprint("docs", __name__, url_prefix="/docs")

_DOCS_DIR = os.path.join(os.getcwd(), "docs")


def _render_md(rel_path: str) -> str:
    safe_path = safe_join(_DOCS_DIR, rel_path)
    if safe_path is None or not safe_path.endswith(".md") or not os.path.isfile(safe_path):
        abort(404)

    with open(safe_path, encoding="utf-8") as f:
        text = f.read()

    html = markdown.markdown(text, extensions=["fenced_code", "tables"])

    # Rewrite relative .md hrefs to /docs/... so links work in the browser
    html = re.sub(
        r'href="(?!https?://|/|#)([^"]+\.md)"',
        r'href="/docs/\1"',
        html,
    )
    return html


@bp.route("/")
def index():
    content = _render_md("README.md")
    return render_template("docs.html", title="Documentação da API", content=content)


@bp.route("/<path:page>")
def page_view(page):
    content = _render_md(page)
    return render_template("docs.html", title="Documentação da API", content=content)
