import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
INITIALIZER = ROOT / "rss_reader" / "static" / "layout-init.js"


def test_saved_split_widths_survive_group_and_arxiv_document_transitions():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")

    harness = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');

function loadRoute(route, viewportWidth, stored) {
  const properties = {};
  const context = {
    document: {
      documentElement: {
        style: { setProperty: (name, value) => { properties[name] = value; } },
      },
    },
    localStorage: { getItem: key => stored[key] ?? null },
    window: { innerWidth: viewportWidth },
    Number,
  };
  vm.runInNewContext(source, context, { filename: 'layout-init.js' });
  return { route, properties };
}

const stored = { rssLeftWidth: '336', rssMiddleWidth: '684' };
const transitions = [
  loadRoute('ordinary-group-a', 1600, stored),
  loadRoute('ordinary-group-b', 1600, stored),
  loadRoute('arxiv-digest', 1600, stored),
];

for (const transition of transitions) {
  if (transition.properties['--left-width'] !== '336px') throw new Error(JSON.stringify(transition));
  if (transition.properties['--middle-width'] !== '684px') throw new Error(JSON.stringify(transition));
}

const narrowed = loadRoute('narrow-viewport', 720, stored).properties;
if (narrowed['--left-width'] !== '324px') throw new Error(JSON.stringify(narrowed));
if (narrowed['--middle-width'] !== '468px') throw new Error(JSON.stringify(narrowed));

const invalid = loadRoute('invalid-storage', 1600, {
  rssLeftWidth: 'not-a-number',
  rssMiddleWidth: '',
}).properties;
if (Object.keys(invalid).length !== 0) throw new Error(JSON.stringify(invalid));

process.stdout.write(JSON.stringify(transitions));
"""
    result = subprocess.run(
        [node, "-e", harness, str(INITIALIZER)],
        check=True,
        capture_output=True,
        text=True,
    )
    transitions = json.loads(result.stdout)
    assert [transition["route"] for transition in transitions] == [
        "ordinary-group-a",
        "ordinary-group-b",
        "arxiv-digest",
    ]
