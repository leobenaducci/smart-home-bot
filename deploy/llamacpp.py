#!/usr/bin/env python3
"""./home-stack llamacpp -- the llama.cpp builds a local model setup can run on.

    ./home-stack llamacpp                 which builds exist, which are pinned
    ./home-stack llamacpp build [flavor]  compile one (vanilla, prism) or all

A setup on the Models page picks an engine: Ollama, or llama.cpp in one of two
flavours. llama.cpp gives each setup its own window per slot, slot count and
KV cache type -- things Ollama applies per *server* -- and PrismML's fork is
the only runtime that reads Bonsai's ternary packings (PQ2_0, PTQ1_0), which
stock llama.cpp and Ollama load as garbage.

Built here rather than downloaded: a release binary targets a generic CPU,
and a model that runs partly on the CPU is only as fast as the instructions
the build uses. `GGML_NATIVE` compiles for this machine's CPU (AVX2/FMA on the
house's Xeon, no AVX-512) and `CMAKE_CUDA_ARCHITECTURES` for its cards. The
build runs in a CUDA devel container, so the host needs no toolkit -- its own
nvcc is 12.0 and too old for either tree.

Each flavour is pinned to a tag; changing a pin is a code change, and a build
lands in its own directory next to the config, so the last good one is never
overwritten. The host helper (ollama_host.py) installs a root-owned copy of the
build a setup names and runs that.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# The flavours a setup may name, pinned. `repo` is cloned at `ref` exactly.
FLAVORS: dict[str, dict[str, str]] = {
    "vanilla": {"label": "llama.cpp", "repo": "https://github.com/ggml-org/llama.cpp",
                "ref": "b11171"},
    # The build the household ran Bonsai 2 on (2026-09-22): "0.2.0-dev (build
    # 10709, commit 9a9394a89)".
    "prism": {"label": "llama.cpp (PrismML)", "repo": "https://github.com/PrismML-Eng/llama.cpp",
              "ref": "prism-b10709-9a9394a"},
}
IMAGE = "nvidia/cuda:12.8.1-devel-ubuntu24.04"
ROOT = Path(os.environ.get("HOME_STACK_LLAMACPP_DIR", "/var/lib/home-stack/llamacpp"))
# Where the host helper installs what a unit runs: root-owned, so a unit never
# executes a file its own user could have replaced.
INSTALLED = Path("/opt/home-stack/llamacpp")
BUILD_RE = re.compile(r"(vanilla|prism)-[A-Za-z0-9._-]{1,60}")


def build_name(flavor: str) -> str:
    return f"{flavor}-{FLAVORS[flavor]['ref']}"


def build_dir(flavor: str, root: Path = ROOT) -> Path:
    return root / build_name(flavor)


def built(flavor: str, root: Path = ROOT) -> dict | None:
    """The build's record, or None when it has not been built (or failed)."""
    try:
        meta = json.loads((build_dir(flavor, root) / "build.json").read_text())
    except (OSError, ValueError):
        return None
    return meta if (build_dir(flavor, root) / "bin" / "llama-server").is_file() else None


def cuda_archs() -> str:
    """The cards' compute capabilities, as CMake wants them: "86" for two 3060s."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    caps = sorted({c.strip().replace(".", "") for c in out.splitlines() if c.strip()})
    return ";".join(c for c in caps if c.isdigit())


def build_script(flavor: str, archs: str, uid: int, gid: int) -> str:
    f = FLAVORS[flavor]
    cuda = (f"-DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES='{archs}'" if archs else "-DGGML_CUDA=OFF")
    # -march=native inside the container is this CPU: a container runs on the
    # host's cores. Shared libs are kept (the server loads them from its own
    # directory through RPATH=$ORIGIN).
    # NCCL off: it is for splitting one model across cards, which no setup
    # here does, and the host has no libnccl -- a build linked to it does not
    # start (2026-09-24). CUDA's own runtime libraries go with the build: the
    # host's are Ubuntu's 12.0, older than the toolkit it was built with.
    return f"""set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq git cmake build-essential libcurl4-openssl-dev ca-certificates >/dev/null
git clone --quiet --depth 1 --branch {f['ref']} {f['repo']} /src
cd /src
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release {cuda} -DGGML_NATIVE=ON \\
  -DLLAMA_CURL=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DGGML_CUDA_NCCL=OFF \\
  -DCMAKE_INSTALL_RPATH='$ORIGIN' -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON >/out/cmake.log
cmake --build build --config Release -j $(nproc) --target llama-server llama-bench >/out/make.log
mkdir -p /out/bin
cp build/bin/llama-server build/bin/llama-bench /out/bin/
find build -name '*.so*' -exec cp -P {{}} /out/bin/ \\;
if [ -d /usr/local/cuda/lib64 ]; then
  cp -P /usr/local/cuda/lib64/libcudart.so.12* /usr/local/cuda/lib64/libcublas.so.12* \\
        /usr/local/cuda/lib64/libcublasLt.so.12* /out/bin/
fi
git rev-parse HEAD > /out/commit
chown -R {uid}:{gid} /out
"""


def build(flavor: str, root: Path = ROOT) -> int:
    if flavor not in FLAVORS:
        print(f"  no flavour {flavor!r}; one of {', '.join(FLAVORS)}")
        return 2
    archs = cuda_archs()
    out = build_dir(flavor, root)
    tmp = out.with_name(out.name + ".building")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    print(f"  building {FLAVORS[flavor]['label']} {FLAVORS[flavor]['ref']} "
          f"(CUDA {archs or 'off'}, native CPU) in {IMAGE} ...")
    started = time.time()
    r = subprocess.run(["docker", "run", "--rm", "-v", f"{tmp}:/out", IMAGE, "bash", "-c",
                        build_script(flavor, archs, os.getuid(), os.getgid())])
    if r.returncode != 0 or not (tmp / "bin" / "llama-server").is_file():
        print(f"  build failed; the logs are in {tmp}")
        return 1
    version = subprocess.run([str(tmp / "bin" / "llama-server"), "--version"], capture_output=True,
                             text=True, env={**os.environ, "LD_LIBRARY_PATH": str(tmp / "bin")})
    # A binary that does not start on this host is not a build: the first one
    # linked a library only the build container had, and --version said so.
    if version.returncode != 0 or "version" not in (version.stdout + version.stderr).lower():
        print(f"  built, but it does not run here:\n    {(version.stderr or version.stdout).strip()[:300]}")
        return 1
    meta = {"flavor": flavor, "ref": FLAVORS[flavor]["ref"], "repo": FLAVORS[flavor]["repo"],
            "commit": (tmp / "commit").read_text().strip(), "cuda_archs": archs,
            "version": (version.stdout + version.stderr).strip().splitlines()[:2],
            "built_at": int(time.time()), "seconds": int(time.time() - started)}
    (tmp / "build.json").write_text(json.dumps(meta, indent=2))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    record_builds(root)
    print(f"  built {build_name(flavor)} in {meta['seconds']} s -> {out}")
    return 0


# What the admin page reads to offer an engine: it runs in a container that
# sees the config directory and not this one.
BUILDS_FILE = Path(os.environ.get("HOME_STACK_CONFIG_DIR", "/var/lib/home-stack/config")) / "llamacpp-builds.json"


def record_builds(root: Path = ROOT, path: Path | None = None) -> None:
    doc = {f: built(f, root) for f in FLAVORS}
    target = path or BUILDS_FILE
    try:
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps({f: m for f, m in doc.items() if m}, indent=1))
        tmp.replace(target)
    except OSError as exc:
        print(f"  (could not write {target}: {exc})")


def report(root: Path = ROOT) -> None:
    for flavor, f in FLAVORS.items():
        meta = built(flavor, root)
        state = (f"built {time.strftime('%Y-%m-%d', time.localtime(meta['built_at']))}, "
                 f"CUDA {meta['cuda_archs'] or 'off'}" if meta else "not built")
        print(f"  {flavor:8} {f['label']:22} {f['ref']:24} {state}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="home-stack llamacpp")
    sub = ap.add_subparsers(dest="cmd")
    b = sub.add_parser("build")
    b.add_argument("flavor", nargs="?", default="all")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        flavors = list(FLAVORS) if args.flavor == "all" else [args.flavor]
        return max(build(f) for f in flavors)
    report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
