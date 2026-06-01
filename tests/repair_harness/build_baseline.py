"""Build a small healthy chroma index for corruption experiments.

Run: .venv/bin/python tests/repair_harness/build_baseline.py [out_dir]

Defaults out_dir to /tmp/chroma_harness/baseline. Creates a 'history'-style
collection with ~500 chunks across ~30 fake source files. Each source file
gets 10–25 lines of synthetic text; chunking uses the same chunk_size=10,
overlap=3 the real pipeline uses.

We need a baseline that:
  - has multiple files (so per-file repair has something to address)
  - has a chunk count similar to a small collection (~500) so iteration is fast
  - uses identical chunking/embedding pipeline as production so any
    findings transfer
"""
import shutil
import sys
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = HARNESS_DIR.parent.parent
sys.path.insert(0, str(PROJECT_DIR / "default-scripts"))

import embed  # noqa: E402


def build(out_dir: Path, num_files: int = 30, lines_per_file: tuple[int, int] = (10, 25)) -> int:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    chroma_dir = out_dir / "chroma"
    chroma_dir.mkdir(parents=True)
    src_dir = out_dir / "src"
    src_dir.mkdir()

    import random
    rng = random.Random(42)

    vocab = [
        "session", "voice", "audio", "transcript", "chunk", "embedding",
        "chroma", "vector", "hnsw", "index", "search", "memory", "history",
        "Jetson", "laptop", "orchestrator", "Claude", "tool", "result",
        "permission", "websocket", "frontend", "backend", "deploy", "test",
    ]
    for i in range(num_files):
        n_lines = rng.randint(*lines_per_file)
        lines = []
        for line_no in range(n_lines):
            n_words = rng.randint(6, 12)
            words = [rng.choice(vocab) for _ in range(n_words)]
            lines.append(f"## Section {line_no}\n" + " ".join(words) + ".")
        (src_dir / f"file_{i:03d}.md").write_text("\n".join(lines))

    embed.INDEX_DIR = chroma_dir
    embed._clients.clear()
    embed._model = None

    embed.index_path(str(src_dir), collection_name="history", chunk_size=10, overlap=3)

    collection = embed.get_collection("history")
    count = collection.count()
    embed._clients.clear()
    return count


def main():
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/chroma_harness/baseline")
    print(f"Building baseline at {out_dir}")
    n = build(out_dir)
    print(f"Baseline ready: {n} chunks in 'history' collection")
    print(f"  chroma path: {out_dir / 'chroma'}")
    print(f"  source files: {out_dir / 'src'}")


if __name__ == "__main__":
    main()
