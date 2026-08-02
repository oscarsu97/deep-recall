"""Split existing mega-cards into independently scheduled sibling cards.

A card written before clustering holds four questions about one mechanism —
mechanism, decision matrix, tipping point, constraint modifiers — under a
single SM-2 ease factor. Rating it Good because the mechanism came back also
pushes the matrix rows you fumbled out to thirty days, so partial failure is
laundered into full success. This cuts each one into its siblings.

No LLM call is involved: the sections already exist, so this is pure surgery on
files you already have.

Every sibling **inherits the parent's review history** rather than starting
over — you have earned those intervals. They therefore all fall due on the same
day at first, which the queue's sibling burying handles by releasing one per
day until they drift apart naturally.

Cards generated before the `subject` field existed have one guessed from the
shape of their question ("How does <subject> <verb>..."), because the sibling
questions are phrased around it and "When is <the whole 20-word parent
question> the wrong answer?" reads badly. Where the guess fails the parent
question is used as the stem: wordier, but never wrong. Either way the next
time you edit that note in Notion, regeneration replaces the guess with a
subject the model wrote.

    python scripts/split_clusters.py            # dry run — prints the plan
    python scripts/split_clusters.py --apply    # perform the split
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config  # noqa: E402
from src.vault import Card, Vault, VaultError, split_card  # noqa: E402

#: "How does X ...", "What is X ...". Everything up to the verb is the subject.
_QUESTION_OPENER = re.compile(
    r"^(?:how|what|when|why|where)\s+(?:does|do|is|are|did|can|will)\s+(?P<rest>.+)$",
    re.IGNORECASE,
)

#: Where the subject stops. Not a parser — just the verbs and prepositions these
#: questions actually use, plus punctuation.
_SUBJECT_END = {
    "achieve", "affect", "avoid", "become", "break", "cause", "change", "choose",
    "consume", "decide", "detect", "determine", "differ", "discover", "drive",
    "encode", "enforce", "ensure", "evaluate", "execute", "expose", "filter",
    "flow", "guarantee", "handle", "happen", "happens", "hold", "identify",
    "implement", "instantiate", "interact", "keep", "know", "locate", "maintain",
    "make", "manage", "map", "move", "operate", "pass", "prevent", "produce",
    "protect", "record", "register", "represent", "resolve", "retry", "scale",
    "serve", "store", "track", "use", "wire", "work", "write",
    "in", "on", "for", "from", "to", "with", "when", "while", "during", "without",
    "against", "across", "between", "over", "under", "and", "that", "which",
    "before", "after", "within", "through", "into", "upon", "per", "at", "by",
}

#: Guesses that are grammatically fine and useless as a subject.
_NOT_A_SUBJECT = {"you", "it", "they", "we", "this", "that", "one", "someone"}
_DANGLING = {"of", "a", "an", "the", "its", "their"}


#: A proper noun, an acronym, an annotation or a backticked identifier — the
#: only tokens confident enough to build four sibling questions on.
_NAMED_THING = re.compile(r"^(?:`[^`]+`|[@A-Z][\w.+#-]*(?:\([A-Z]+\))?)$")


def infer_subject(question: str) -> str:
    """Guess the noun phrase a card is about. Empty when the shape does not fit.

    Deliberately quick to give up. A wrong guess corrupts all four sibling
    questions, while giving up just falls back to the parent question — so this
    accepts only names it can be sure of ("Aeron", "Spring Boot Actuator",
    "`@Configuration`", "UDP") and rejects anything where a verb may have crept
    in ("data pipeline be designed", "ringbuffers help"). On the vault this was
    written for that is about a fifth of cards; the rest read fine on the
    fallback and get a real subject the next time their note is edited.
    """
    match = _QUESTION_OPENER.match(question.strip())
    if not match:
        return ""

    words: list[str] = []
    for raw in match.group("rest").split():
        token = raw.strip(",;:?")
        if token.lower().strip("`'\"") in _SUBJECT_END or raw.endswith((",", ";", ":")):
            break
        words.append(token)
        if len(words) == 4:
            break

    while words and words[0].lower() in {"a", "an", "the"}:
        words.pop(0)
    if not words or len(words) > 3:
        return ""
    if words[-1].lower() in _DANGLING or words[0].lower() in _NOT_A_SUBJECT:
        return ""
    if not all(_NAMED_THING.match(w) for w in words):
        return ""
    if len(words) == 1 and len(words[0]) < 3:
        return ""
    return " ".join(words)


def plan(vault: Vault) -> list[tuple[Card, list[Card]]]:
    """`(parent, siblings)` for every card that splits into more than one."""
    out: list[tuple[Card, list[Card]]] = []
    for card in vault.load_all():
        if card.kind:
            continue  # already a sibling
        if not card.meta.get("subject"):
            guessed = infer_subject(card.question)
            if guessed:
                card.meta["subject"] = guessed
        siblings = split_card(card, fresh_schedule=False)
        if len(siblings) > 1:
            out.append((card, siblings))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the split")
    parser.add_argument("--vault", type=Path, default=None, help="override VAULT_DIR")
    args = parser.parse_args()

    root = args.vault or Config.load().vault_dir
    vault = Vault(root)
    is_git = (root / ".git").exists()

    work = plan(vault)
    if not work:
        print("Every card is already split. Nothing to do.")
        return 0

    for parent, siblings in work:
        assert parent.path is not None
        print(f"\n  {parent.path.relative_to(root)}")
        for sibling in siblings:
            words = len(sibling.answer_body().split())
            print(f"      -> {sibling.id:48} {sibling.kind:10} {words:3} words")

    before = sum(len(p.answer_body().split()) for p, _ in work)
    after = sum(len(s.answer_body().split()) for _, ss in work for s in ss)
    print(
        f"\n{len(work)} card(s) -> {sum(len(s) for _, s in work)} sibling(s). "
        f"Mean answer length {before // len(work)} -> "
        f"{after // sum(len(s) for _, s in work)} words."
    )

    if not args.apply:
        print("\nDry run. Re-run with --apply to perform the split.")
        return 0

    written = 0
    removed = 0
    for parent, siblings in work:
        assert parent.path is not None
        try:
            # Write every sibling before removing the parent: a crash in the
            # middle must leave the original card intact, not half a cluster.
            for sibling in siblings:
                vault.save(sibling)
                written += 1
        except VaultError as exc:
            print(f"  ! could not split {parent.id}: {exc}")
            continue

        if _remove(parent.path, root, is_git):
            removed += 1

    print(f"\nApplied: {written} sibling(s) written, {removed} parent card(s) removed.")
    print(f"Review with `git -C {root} status`, then commit.")
    return 0


def _remove(path: Path, root: Path, is_git: bool) -> bool:
    """`git rm` where possible so the deletion is staged along with the split."""
    if is_git:
        result = subprocess.run(
            ["git", "rm", "--quiet", "--", str(path.relative_to(root))],
            cwd=root, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            return True
        print(f"  ! git rm failed for {path.name}: {result.stderr.strip()}")

    try:
        path.unlink()
    except OSError as exc:
        print(f"  ! could not remove {path.name}: {exc}")
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
