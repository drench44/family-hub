"""Remake the pictures of a recipe file on a ComfyUI machine.

    python run_recipes.py art/recipes/kitchen-scenes.json --gen C:/ComfyUI/gen.py --out out/ [names...]

Runs on the machine with ComfyUI and its `gen.py` helper (two modes used here:
`gen.py klein` for FLUX.2 klein 4B and `gen.py upscale` for SeedVR2, both
Apache 2.0). For each picture: generate at the recipe's size and seed, then
upscale 2x with seed 1, and save <out>/<output>.png. It skips pictures already
in <out> unless you name them. A failure is logged and the rest go on.
Same prompt, seed, size, model files and ComfyUI version give the same picture.
"""
import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time


def gen(py, script, *args):
    r = subprocess.run([py, script, *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-400:])
    return r.stdout.strip().splitlines()[-1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recipes")
    ap.add_argument("--gen", required=True, help="path to ComfyUI's gen.py")
    ap.add_argument("--python", default=sys.executable, help="the Python that runs gen.py (ComfyUI's venv)")
    ap.add_argument("--input", help="ComfyUI's input folder (default: the input folder beside gen.py)")
    ap.add_argument("--out", required=True)
    ap.add_argument("names", nargs="*", help="only these outputs (and redo them even if present)")
    a = ap.parse_args()
    doc = json.loads(pathlib.Path(a.recipes).read_text(encoding="utf-8"))
    g = doc["generator"]
    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    inp = pathlib.Path(a.input) if a.input else pathlib.Path(a.gen).parent / "input"
    work = inp / "family-hub-art"
    work.mkdir(parents=True, exist_ok=True)
    failed = 0
    for p in doc["pictures"]:
        name = p["output"]
        if a.names and name not in a.names:
            continue
        if (out / f"{name}.png").exists() and not a.names:
            continue
        t = time.time()
        try:
            pf = work / f"{name}.txt"
            pf.write_text(p["prompt"], encoding="utf-8")
            base = gen(a.python, a.gen, "klein", "--prompt-file", str(pf), "--width", str(g["width"]),
                       "--height", str(g["height"]), "--seed", str(p["seed"]))
            shutil.copy(base, work / f"{name}-base.png")
            up = gen(a.python, a.gen, "upscale", "--image", f"family-hub-art/{name}-base.png", "--seed", "1")
            shutil.copy(up, out / f"{name}.png")
            print(f"ok {name} {time.time() - t:.0f}s", flush=True)
        except Exception as e:   # one bad picture must not stop the batch; it is reported and counted
            failed += 1
            print(f"FAIL {name}: {e}", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
