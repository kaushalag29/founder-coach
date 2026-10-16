"""Step 3a of the Laya spike: fetch Laya's files at the pinned revision, code first.

    python spikes/laya/fetch_model.py             # the file list, code, configs and tokenizer (no weights)
    python spikes/laya/fetch_model.py --weights   # after the code review passes: the English checkpoint

Never a moving ref (common.LAYA_REVISION), never the `laya` pip package, nothing imported or run:
this only writes files under data/spikes/laya/model/ and FILES.txt, the repository's full listing.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import LAYA_REPO, LAYA_REVISION, MODEL_DIR  # noqa: E402

CODE = ["*.py", "*.json", "*.md", "*.txt", "tokenizer/*", "encoder/*.json", "encoder/*.txt", "encoder/*.py"]
WEIGHTS = ["model.safetensors", "encoder/*", "tokenizer/*"]


def _sha(lfs) -> str:
    if lfs is None:
        return ""
    return (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", "")) or ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", action="store_true", help="also the English checkpoint (~0.9 GB)")
    a = ap.parse_args()
    from huggingface_hub import HfApi, snapshot_download

    info = HfApi().model_info(LAYA_REPO, revision=LAYA_REVISION, files_metadata=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    listing = [f"{s.rfilename}\t{s.size or 0}\t{_sha(s.lfs)}" for s in info.siblings]
    (MODEL_DIR / "FILES.txt").write_text(f"{LAYA_REPO}@{info.sha}\n" + "\n".join(sorted(listing)) + "\n")
    if info.sha != LAYA_REVISION:
        print(f"laya fetch: the Hub resolved {LAYA_REVISION} to {info.sha}; stopping", file=sys.stderr)
        return 1
    snapshot_download(LAYA_REPO, revision=LAYA_REVISION, local_dir=MODEL_DIR,
                      allow_patterns=CODE + (WEIGHTS if a.weights else []),
                      ignore_patterns=[] if a.weights else ["*.safetensors", "*.bin", "*.pt", "*.onnx", "*.gguf"])
    print(f"laya fetch: {LAYA_REPO}@{LAYA_REVISION[:7]} -> {MODEL_DIR} "
          f"({'code, configs and weights' if a.weights else 'code and configs only'}); listing in FILES.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
