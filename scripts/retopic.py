"""Re-file existing vault cards onto the canonical topic vocabulary.

Run after editing `src/topics.py` to move cards whose topic is no longer
canonical. Uses `git mv` so history follows the file, and rewrites the `topic:`
frontmatter to match the new folder.

Review state lives in the same frontmatter and is untouched, so a card keeps
its SM-2 schedule across the move.

    python scripts/retopic.py            # dry run — prints the plan
    python scripts/retopic.py --apply    # perform the moves
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import topics  # noqa: E402
from src.config import Config  # noqa: E402
from src.vault import Vault, slugify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the moves")
    parser.add_argument("--vault", type=Path, default=None, help="override VAULT_DIR")
    args = parser.parse_args()

    root = args.vault or Config.load().vault_dir
    vault = Vault(root)
    is_git = (root / ".git").exists()

    moves: list[tuple[Path, Path, str, str]] = []
    rewrites: list[tuple[Path, str, str]] = []

    for card in vault.load_all():
        target_topic = topics.canonical(card.topic)
        if target_topic == card.topic:
            continue
        source = card.path
        assert source is not None
        target = root / slugify(target_topic) / source.name
        if target == source:
            # Same folder, different label (e.g. "Databases" vs "databases").
            rewrites.append((source, card.topic, target_topic))
        else:
            moves.append((source, target, card.topic, target_topic))

    if not moves and not rewrites:
        print("Every card is already on a canonical topic. Nothing to do.")
        return 0

    for source, target, old, new in moves:
        print(f"  {old:26} -> {new:22} {source.relative_to(root)} -> {target.relative_to(root)}")
    for source, old, new in rewrites:
        print(f"  {old:26} -> {new:22} {source.relative_to(root)} (frontmatter only)")

    print(f"\n{len(moves)} move(s), {len(rewrites)} frontmatter-only rewrite(s).")
    if not args.apply:
        print("Dry run. Re-run with --apply to perform them.")
        return 0

    for source, target, _old, new in moves:
        target.parent.mkdir(parents=True, exist_ok=True)
        if is_git:
            result = subprocess.run(
                ["git", "mv", str(source.relative_to(root)), str(target.relative_to(root))],
                cwd=root, capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                print(f"  ! git mv failed for {source.name}: {result.stderr.strip()}")
                continue
        else:
            source.rename(target)
        _set_topic(target, new)

    for source, _old, new in rewrites:
        _set_topic(source, new)

    # Folders emptied by the moves would otherwise linger in Obsidian's sidebar.
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and folder.name != ".git" and not any(folder.iterdir()):
            folder.rmdir()
            print(f"  removed empty folder {folder.name}/")

    print(f"\nApplied. Review with `git -C {root} status`, then commit.")
    return 0


def _set_topic(path: Path, topic: str) -> None:
    """Rewrite the `topic:` frontmatter line in place, leaving all else alone."""
    lines = path.read_text(encoding="utf-8").split("\n")
    for i, line in enumerate(lines):
        if line.startswith("topic:"):
            lines[i] = f'topic: "{topic}"'
            break
    path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
