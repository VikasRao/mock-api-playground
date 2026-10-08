"""Admin page smoke tests: it renders, has the progress elements, and its
inline script is valid JavaScript (checked with node when available)."""

import re
import shutil
import subprocess

import pytest


def admin_html(client):
    resp = client.get("/admin")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_admin_page_has_progress_elements(client):
    html = admin_html(client)
    assert 'id="busy-pill"' in html
    assert 'id="busy-overlay"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_admin_page_script_is_valid_javascript(client, tmp_path):
    scripts = re.findall(r"<script>(.*?)</script>", admin_html(client), re.S)
    assert scripts, "no inline <script> found"
    js = tmp_path / "admin.js"
    js.write_text("\n".join(scripts))
    result = subprocess.run(["node", "--check", str(js)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_body_editor_has_selection_label(client):
    assert 'id="editor-label-items_list"' in admin_html(client)


def test_endpoint_rows_are_draggable_onto_categories(client):
    html = admin_html(client)
    assert 'draggable="true"' in html
    assert 'ondrop="dropOnCategory(event, this)"' in html
    assert "Move to category" in html
