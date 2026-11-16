# histfactory-bench

Collects the public pyhf HistFactory workspaces listed at
<https://pyhf.github.io/public-probability-models/> and checks that they load. They are
meant as input for a lightweight tool to benchmark workspaces, which does not exist yet.

## Usage

Requires [pixi](https://pixi.sh).

```
pixi run fetch                   # download everything into data/ (190 MB)
pixi run validate                # load every workspace (about 25 minutes)
pixi run validate SUSY-2019-08   # only selected analyses
```

The index only links to HEPData records, `fetch.py` downloads the likelihood resources
attached to them, unpacks archives and sorts the JSON files by content. They are stored
gzip-compressed (about 20x smaller for pyhf workspaces, and decompression is fast
compared to parsing the JSON), the downloads themselves are not kept. Rerunning it only
downloads what is missing.

| path | content |
| --- | --- |
| `data/pyhf/<ANALYSIS>/` | pyhf workspaces and patchsets (`.json.gz`) |
| `data/hs3/<ANALYSIS>/` | HS3 JSON (`.json.gz`), not supported by pyhf |
| `data/cache/` | bookkeeping of processed HEPData resources |
| `manifest.json` | per analysis: HEPData record, resources, files, validation result |

`data/` is not part of the repository, `manifest.json` is. Sizes and checksums in the
manifest refer to the uncompressed files as published.

```python
import gzip, json, pyhf

with gzip.open("data/pyhf/TOPQ-2018-05/workspace.json.gz") as f:
    workspace = pyhf.Workspace(json.load(f))
```

For the command line: `gunzip -c <file> > workspace.json && pyhf inspect workspace.json`.

## Validation

`validate.py` runs `pyhf inspect` on each workspace, builds the model and evaluates the
log-likelihood at the initial parameters. Background-only workspaces are patched with
the first patch of their patchset beforehand, matched via the workspace digest in the
patchset metadata.

| status | meaning |
| --- | --- |
| `ok` | loads, log-likelihood at initial parameters is finite |
| `no-poi` | loads, but the POI of the measurement is not a model parameter, so `pyhf inspect` needs the POI to be overridden |
| `non-finite` | loads, but the log-likelihood at initial parameters is nan or -inf |
| `failed` | does not load |

Published files are never modified, defects are recorded as found.

## Results

As of 2026-11-16 with pyhf 0.8.0. The index lists 45 records:

- 28 with pyhf workspaces, 468 files
- 11 with HS3 only
- 5 without a likelihood on HEPData: TOPQ-2019-03, TOPQ-2019-22, HDBS-2018-34,
  SUSY-2019-04, EXOT-2022-04
- TOPQ-2019-02: `workspace.json` is a Python dict dump, not valid JSON

No workspace fails to load:

| status | files | where |
| --- | --- | --- |
| `ok` | 130 | |
| `no-poi` | 82 | TOPQ-2018-24 (62, several POIs in one string); background-only workspaces without patchset in SUSY-2018-22 (18), SUSY-2018-05 (1), SUSY-2018-41 (1) |
| `non-finite` | 256 | TOPQ-2021-08 (1) and TOPQ-2020-10 (2): `normsys` with factor 0; EXOT-2019-15 (253): only the signal sample is left, also after applying the patchset |
