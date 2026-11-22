"""Packaging verification tests for the v0.2.0 Antigravity plugin."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET

_REPO_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN_JSON_PATH = _REPO_ROOT / "plugin.json"
_MCP_CONFIG_PATH = _REPO_ROOT / "mcp_config.json"
_LOGO_SVG_PATH = _REPO_ROOT / "assets" / "logo.svg"
_SKILL_MD_PATH = _REPO_ROOT / "skills" / "resume-session" / "SKILL.md"
_README_MD_PATH = _REPO_ROOT / "README.md"

_ALLOWED_PLUGIN_KEYS = frozenset(
    {
        "name",
        "displayName",
        "version",
        "description",
        "logo",
        "suggestedPrompts",
        "author",
        "repository",
    }
)


def _parse_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", markdown, re.DOTALL)
    if not match:
        raise AssertionError("SKILL.md must begin with YAML frontmatter delimited by ---")
    raw_yaml, body = match.group(1), match.group(2)
    fields: dict[str, str] = {}
    for line in raw_yaml.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise AssertionError(f"Unsupported YAML frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        fields[key.strip()] = value.strip()
    return fields, body


class TestPluginPackaging(unittest.TestCase):
    """Verify plugin.json, mcp_config.json, logo.svg, SKILL.md, README.md, and agy validation."""

    def test_plugin_manifest_mcp_config_logo_and_skill_frontmatter(self) -> None:
        self.assertTrue(_PLUGIN_JSON_PATH.is_file(), f"Missing {_PLUGIN_JSON_PATH}")
        manifest = json.loads(_PLUGIN_JSON_PATH.read_text(encoding="utf-8"))
        self.assertIsInstance(manifest, dict)
        self.assertTrue(
            set(manifest.keys()).issubset(_ALLOWED_PLUGIN_KEYS),
            f"Unexpected top-level keys in plugin.json: {set(manifest.keys()) - _ALLOWED_PLUGIN_KEYS}",
        )
        self.assertEqual(manifest.get("name"), "agy-session-importer")
        self.assertEqual(manifest.get("displayName"), "Antigravity Session Importer")
        self.assertEqual(manifest.get("version"), "0.2.0")
        self.assertEqual(manifest.get("logo"), "assets/logo.svg")
        self.assertEqual(
            manifest.get("repository"),
            "https://github.com/leihanchen/agy_session_importer",
        )
        self.assertIsInstance(manifest.get("description"), str)
        self.assertTrue(manifest["description"].strip())
        prompts = manifest.get("suggestedPrompts")
        self.assertIsInstance(prompts, list)
        self.assertGreaterEqual(len(prompts), 1)
        self.assertLessEqual(len(prompts), 3)
        for prompt in prompts:
            self.assertIsInstance(prompt, str)
            self.assertTrue(prompt.strip())
        self.assertIn("author", manifest)

        # Logo SVG exists, is valid XML/SVG, and has a 128x128 square viewBox
        self.assertTrue(_LOGO_SVG_PATH.is_file(), f"Missing {_LOGO_SVG_PATH}")
        svg_text = _LOGO_SVG_PATH.read_text(encoding="utf-8")
        svg_root = ET.fromstring(svg_text)
        self.assertTrue(
            svg_root.tag.endswith("svg"),
            f"Expected <svg> root element, got {svg_root.tag!r}",
        )
        self.assertEqual(svg_root.attrib.get("viewBox"), "0 0 128 128")
        self.assertEqual(svg_root.attrib.get("width"), "128")
        self.assertEqual(svg_root.attrib.get("height"), "128")

        # mcp_config.json points to ${PLUGIN_ROOT}/skills/resume-session/scripts/mcp_server.py and ${PLUGIN_DATA}
        self.assertTrue(_MCP_CONFIG_PATH.is_file(), f"Missing {_MCP_CONFIG_PATH}")
        mcp_cfg = json.loads(_MCP_CONFIG_PATH.read_text(encoding="utf-8"))
        servers = mcp_cfg.get("mcpServers")
        self.assertIsInstance(servers, dict)
        self.assertIn("session_importer", servers)
        srv = servers["session_importer"]
        self.assertEqual(srv.get("command"), "python3")
        args = srv.get("args")
        self.assertEqual(
            args,
            [
                "${PLUGIN_ROOT}/skills/resume-session/scripts/mcp_server.py",
                "--data-dir",
                "${PLUGIN_DATA}",
            ],
        )

        # SKILL.md frontmatter strictly {"name", "description"} and orchestrates MCP + CLI fallback
        self.assertTrue(_SKILL_MD_PATH.is_file(), f"Missing {_SKILL_MD_PATH}")
        skill_text = _SKILL_MD_PATH.read_text(encoding="utf-8")
        fm, skill_body = _parse_frontmatter(skill_text)
        self.assertEqual(set(fm.keys()), {"name", "description"})
        self.assertEqual(fm["name"], "resume-session")
        self.assertTrue(fm["description"])

        for required_token in (
            "open_session_importer",
            "render_imported_conversation",
            "import_session_context",
            "session_importer.py",
            "import",
            "--no-spawn",
            "untrusted_inert_history",
            "None — cancel without importing",
            "--allow-cross-repo",
            "providers --json",
            "turns --range",
        ):
            self.assertIn(required_token, skill_body)

        # README.md documents v0.2 UI entrypoints, duplicate skill cleanup, CLI import/imports, and provider guide
        self.assertTrue(_README_MD_PATH.is_file(), f"Missing {_README_MD_PATH}")
        readme = _README_MD_PATH.read_text(encoding="utf-8")
        for doc_token in (
            "Session Importer",
            "render_imported_conversation",
            "/resume-session",
            "mcp_config.json",
            "import",
            "imports",
            "~/.gemini/config/skills/resume-session",
            "create_default_registry()",
        ):
            self.assertIn(doc_token, readme)

        # Strict agy plugin validate check when agy CLI is installed
        agy_bin = shutil.which("agy")
        if agy_bin:
            proc = subprocess.run(
                [agy_bin, "plugin", "validate", str(_REPO_ROOT), "--strict"],
                capture_output=True,
                text=True,
                check=False,
            )
            combined = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, f"agy plugin validate failed:\n{combined}")
            self.assertIn("skills 1", proc.stdout)
            self.assertIn("mcpServers 1", proc.stdout)
            self.assertIn("Validation passed", proc.stdout)


if __name__ == "__main__":
    unittest.main()
