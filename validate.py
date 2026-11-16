"""Check that every downloaded pyhf workspace loads, record the outcome in manifest.json.

Background-only workspaces that ship with a patchset are validated with the first
signal patch applied. Each workspace is handled in its own process. Status:
  ok          pyhf inspect works, model builds, logpdf at initial parameters is finite
  no-poi      loads, but the POI of the measurement is not a model parameter
              (background-only without patchset, or several POIs in one string)
  non-finite  loads, but logpdf at initial parameters is nan / inf
  failed      does not load
"""

import gzip
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"
TIMEOUT = 1800


def read(path):
    return json.loads(gzip.decompress(Path(ROOT / path).read_bytes()))


def patchset_digests(analysis):
    """Map workspace digest -> patchsets written for it, patchsets can sit in other folders."""
    digests = {}
    for entry in analysis["files"]:
        if entry["kind"] == "patchset":
            metadata = read(entry["path"])["metadata"]
            digests.setdefault(metadata.get("digests", {}).get("sha256"), []).append(entry["path"])
    return digests


def worker(path, digests):
    import pyhf

    result = {"status": "failed", "pyhf": pyhf.__version__}
    stage = "read"
    try:
        spec = read(path)

        stage = "patch"
        patchsets = sorted(digests.get(pyhf.utils.digest(spec), []))
        if patchsets:
            patchset = pyhf.PatchSet(read(patchsets[0]))
            patch_name = patchset.patches[0].name
            spec = patchset.apply(spec, patch_name)
            result |= {"patchset": patchsets[0], "patch": patch_name, "n_patchsets": len(patchsets)}

        stage = "inspect"
        # the pyhf command line needs an uncompressed file
        inspect_target = ROOT / "data" / "tmp" / f"inspect-{time.time_ns()}.json"
        inspect_target.parent.mkdir(parents=True, exist_ok=True)
        inspect_target.write_text(json.dumps(spec))
        no_poi = False
        try:
            pyhf_cli = Path(sys.executable).parent / "pyhf"
            subprocess.run(
                [pyhf_cli, "inspect", inspect_target], check=True, capture_output=True, text=True
            )
        except subprocess.CalledProcessError as e:
            error = e.stderr.strip().splitlines()[-1]
            # background-only workspace published without signal: loads, but has no POI
            no_poi = "parameter of interest" in error and "not declared" in error
            if not no_poi:
                raise RuntimeError(error) from None
            result["inspect_error"] = error[:500]
        finally:
            inspect_target.unlink(missing_ok=True)

        stage = "model"
        workspace = pyhf.Workspace(spec)
        model = workspace.model(poi_name=None) if no_poi else workspace.model()
        data = workspace.data(model)
        result |= {
            "channels": len(model.config.channels),
            "bins": sum(model.config.channel_nbins.values()),
            "samples": len(model.config.samples),
            "parameters": model.config.npars,
            "modifiers": len(model.config.modifiers),
            "poi": model.config.poi_name,
        }

        stage = "logpdf"
        logpdf = float(model.logpdf(model.config.suggested_init(), data)[0])
        if not math.isfinite(logpdf):
            # loads fine, but is not usable for fits as published
            result |= {"status": "non-finite", "logpdf_init": str(logpdf)}
        else:
            result["status"] = "no-poi" if no_poi else "ok"
    except Exception as e:
        result |= {"stage": stage, "error": f"{type(e).__name__}: {e}"[:500]}
    print(json.dumps(result))


def main():
    manifest = json.loads(MANIFEST.read_text())
    only = sys.argv[1:]  # optional analysis names
    failed = 0
    for name, analysis in manifest["analyses"].items():
        if only and name not in only:
            continue
        digests = json.dumps(patchset_digests(analysis))
        for entry in analysis["files"]:
            if entry["kind"] != "workspace":
                continue
            try:
                run = subprocess.run(
                    [sys.executable, "-I", __file__, "--worker", ROOT / entry["path"], digests],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT,
                )
                result = json.loads(run.stdout.strip().splitlines()[-1])
            except subprocess.TimeoutExpired:
                result = {"status": "failed", "stage": "timeout", "error": f"> {TIMEOUT} s"}
            except (IndexError, ValueError):
                error = run.stderr.strip().splitlines()[-1:] or [f"exit code {run.returncode}"]
                result = {"status": "failed", "stage": "crash", "error": error[0][:500]}
            entry["validation"] = result
            failed += result["status"] == "failed"
            detail = result.get("error") or result.get("logpdf_init") and f"logpdf at init is {result['logpdf_init']}" or (
                f"{result['channels']} channels, {result['bins']} bins, "
                f"{result['parameters']} parameters" + (f", patch {result['patch']}" if "patch" in result else "")
            )
            print(f"{result['status']:6} {entry['path']}  [{detail}]", flush=True)
        MANIFEST.write_text(json.dumps(manifest, indent=1) + "\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        worker(Path(sys.argv[2]), json.loads(sys.argv[3]))
    else:
        main()
