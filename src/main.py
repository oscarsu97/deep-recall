"""DeepRecall CLI.

    python -m src.main --sync              # Notion -> LLM -> vault/*.md
    python -m src.main --chat-id           # find your Telegram chat id
    python -m src.main --notify            # push today's due cards to Telegram
    python -m src.main --bot-poll          # serve the inline-button flow
    python -m src.main --stats             # vault overview
    python -m src.main --review <id> --quality good   # rate without Telegram

Every command exits non-zero on failure so GitHub Actions surfaces problems
instead of silently producing an empty vault.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from .config import Config, ConfigError, REPO_ROOT
from .sm2 import QUALITY_EASY, QUALITY_GOOD, QUALITY_HARD, humanise_interval, review
from .vault import Vault, VaultError

log = logging.getLogger("deeprecall")

QUALITY_BY_NAME = {
    "hard": QUALITY_HARD, "1": QUALITY_HARD,
    "good": QUALITY_GOOD, "3": QUALITY_GOOD,
    "easy": QUALITY_EASY, "5": QUALITY_EASY,
}


def _display(path: Path) -> Path:
    """Shorten a path for logging — the vault may sit outside the code repo."""
    try:
        return path.relative_to(REPO_ROOT)
    except ValueError:
        return path


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "telegram.ext.Application", "notion_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _plan_sync(
    vault: Vault, notes: list, force: bool = False, backfill: bool = True
) -> tuple[list[tuple], int]:
    """Decide, per ingested note, whether to create, regenerate, or skip.

    Returns `(pending, backfilled)` where `pending` is a list of
    `(note, existing_siblings)` — the cards already cut from that note, empty
    for a note seen for the first time — and `backfilled` counts pre-existing
    cards that gained provenance metadata without needing an LLM call.

    A note is matched to its cards by Notion block id first (stable when you
    reword the question) and by question slug second (cards created before
    block ids were recorded). One note now yields several sibling cards, so
    what comes back is the whole cluster: each sibling carries its own SM-2
    state across a regeneration, and a sibling with no counterpart in the new
    output has to be pruned rather than orphaned.
    """
    if force:
        return [(note, []) for note in notes], 0

    index = vault.index_clusters_by_source()
    pending: list[tuple] = []
    backfilled = 0

    for note in notes:
        siblings = (index.get(note.block_id) if note.block_id else None) or []
        siblings = siblings or index.get(note.suggested_id) or []

        if not siblings:
            pending.append((note, []))
            continue

        # Siblings are written together, so any of them answers for the set.
        stored_hash = next(
            (s.meta.get("source_hash") for s in siblings if s.meta.get("source_hash")), None
        )
        if not stored_hash:
            # Cards predate content hashing. We cannot tell whether the note
            # changed, so assume not — regenerating everything on upgrade would
            # be a nasty surprise — but record the hash so edits are caught from
            # now on.
            if not backfill:
                log.debug("• would backfill provenance: %s", siblings[0].id)
                continue
            for card in siblings:
                card.meta["source_hash"] = note.content_hash
                if note.block_id:
                    card.meta.setdefault("source_block_id", note.block_id)
                card.meta.setdefault("body_hash", card.body_digest())
                try:
                    vault.save(card)
                    backfilled += 1
                except VaultError as exc:
                    log.warning("Could not backfill provenance for %s: %s", card.id, exc)
            continue

        if str(stored_hash) == note.content_hash:
            log.debug("• unchanged: %s", siblings[0].id)
            continue

        edited = [s for s in siblings if s.is_hand_edited()]
        if edited:
            log.warning(
                "• %s changed in Notion, but %s was edited in Obsidian — "
                "skipping the whole cluster to protect your edits (use --force to overwrite).",
                siblings[0].cluster or siblings[0].id, edited[0].id,
            )
            continue

        pending.append((note, siblings))

    return pending, backfilled


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    """Ingest recent Notion notes and synthesise them into vault cards."""
    from .ingestion import IngestionError, NotionIngestor
    from .synthesizer import QuotaExhaustedError, SynthesisError, Synthesizer

    config.require_notion()
    if not args.dry_run:
        config.require_llm()

    vault = Vault(config.vault_dir)
    vault.ensure()

    try:
        ingestor = NotionIngestor(config)
        notes = ingestor.fetch_notes(lookback_hours=args.hours)
    except IngestionError as exc:
        log.error("Ingestion failed: %s", exc)
        return 1

    if not notes:
        log.info("No new Q&A notes found. Nothing to synthesise.")
        return 0

    pending, backfilled = _plan_sync(
        vault, notes, force=args.force, backfill=not args.dry_run
    )

    if backfilled and not args.dry_run:
        log.info("Backfilled provenance on %d pre-existing card(s)", backfilled)

    if args.limit:
        pending = pending[: args.limit]

    new_count = sum(1 for _, existing in pending if not existing)
    log.info(
        "%d note(s) to synthesise — %d new, %d changed (%d ingested)",
        len(pending), new_count, len(pending) - new_count, len(notes),
    )

    if args.dry_run:
        for note, existing in pending:
            marker = "NEW" if not existing else f"CHANGED -> {existing[0].cluster or existing[0].id}"
            print(f"\n--- {note.suggested_id} [{note.suggested_topic}]  ({marker})")
            print(f"Q: {note.question}")
            print(note.raw_answer[:500])
        return 0

    if not pending:
        return 0

    synthesizer = Synthesizer(config)
    written: list[Path] = []
    removed: list[Path] = []
    failures = 0
    quota_hit = False

    for i, (note, existing) in enumerate(pending, 1):
        verb = "synthesising" if not existing else "regenerating"
        log.info("[%d/%d] %s: %s", i, len(pending), verb, note.question[:70])
        try:
            cards = synthesizer.synthesize(note)
        except QuotaExhaustedError as exc:
            # Every remaining note would fail identically; stop rather than
            # spend the next ten minutes proving it.
            log.error("  ✗ %s", exc)
            log.error(
                "Stopped after %d card(s). %d note(s) left — they stay in Notion "
                "and will be picked up by the next sync.",
                len(written), len(pending) - i + 1,
            )
            quota_hit = True
            break
        except SynthesisError as exc:
            # One bad note must not abandon the rest of the batch.
            log.error("  ✗ %s", exc)
            failures += 1
            continue

        # Same note, new content: each sibling keeps its own id, file location
        # and schedule, matched to its counterpart by kind.
        previous = {c.sibling_key: c for c in existing}
        for card in cards:
            twin = previous.pop(card.sibling_key, None)
            if twin is not None:
                card.carry_review_state_from(twin)
                card.meta["revised"] = date.today().isoformat()

        wrote_any = False
        for card in cards:
            try:
                path = vault.save(card)
            except VaultError as exc:
                log.error("  ✗ could not write %s: %s", card.id, exc)
                failures += 1
                continue
            log.info("  ✓ %s", _display(path))
            written.append(path)
            wrote_any = True

        # Siblings with no counterpart in the new output — the note lost a
        # constraint modifier, say. Leaving them behind would keep scheduling a
        # question the note no longer answers. Only prune once the replacements
        # are safely on disk.
        if wrote_any:
            for orphan in previous.values():
                try:
                    path = vault.delete(orphan)
                except VaultError as exc:
                    log.warning("  ! could not remove stale sibling %s: %s", orphan.id, exc)
                    continue
                if path:
                    log.info("  – removed stale sibling %s", _display(path))
                    removed.append(path)

    if (written or removed) and config.git_auto_commit:
        from . import git_sync

        git_sync.commit_paths(
            written,
            f"sync: add {len(written)} card(s) from Notion ({date.today().isoformat()})",
            repo=config.vault_dir,
            push=not args.no_push,
            removals=removed,
        )

    log.info("Sync complete: %d card(s) written, %d removed, %d failure(s)%s",
             len(written), len(removed), failures,
             " (stopped early: quota)" if quota_hit else "")
    # Partial success is still success; only a total wipe-out is an error.
    return 1 if failures and not written else 0


def cmd_notify(config: Config, args: argparse.Namespace) -> int:
    from .telegram_bot import notify

    config.require_telegram()
    sent = notify(config, limit=args.limit)
    log.info("Delivered %d card(s).", sent)
    return 0


def cmd_chat_id(config: Config, _: argparse.Namespace) -> int:
    """Find your Telegram chat id without starting the polling application."""
    from .telegram_bot import resolve_chat_id

    return 0 if resolve_chat_id(config) is not None else 1


def cmd_bot_poll(config: Config, args: argparse.Namespace) -> int:
    from .telegram_bot import poll

    config.require_telegram(need_chat_id=False)
    poll(config, duration_seconds=args.duration)
    return 0


def cmd_stats(config: Config, _: argparse.Namespace) -> int:
    vault = Vault(config.vault_dir)
    stats = vault.stats()
    # What `--notify` would actually deliver, which is the number that matters:
    # the raw backlog counts every sibling of every cluster and reads as alarming.
    due = vault.due_cards(
        limit=config.max_cards_per_session, new_limit=config.max_new_cards_per_session
    )

    print(f"\n📊 DeepRecall vault — {config.vault_dir}")
    print(f"   cards         {stats['total']}  across {stats['topics']} topic(s), "
          f"{stats['clusters']} cluster(s)")
    print(f"   next session  {len(due)}  (of {stats['due']} due — one per cluster, "
          f"max {config.max_new_cards_per_session} new)")
    print(f"   never seen    {stats['new']}")
    print(f"   mature ≥21d   {stats['mature']}\n")

    for card in due[:20]:
        state = card.review_state
        print(f"   • [{card.topic}] {card.question[:66]}")
        print(f"     due {state.next_review}  ease {state.ease_factor:.2f}  "
              f"rep {state.repetition_count}")
    if len(due) > 20:
        print(f"   … and {len(due) - 20} more")
    print()
    return 0


def cmd_review(config: Config, args: argparse.Namespace) -> int:
    """Rate a card from the terminal — the same path the bot's buttons take."""
    quality = QUALITY_BY_NAME.get(str(args.quality).lower())
    if quality is None:
        log.error("--quality must be one of: hard, good, easy")
        return 2

    vault = Vault(config.vault_dir)
    card = vault.find(args.review)
    if card is None:
        log.error("No card with id %r in %s", args.review, config.vault_dir)
        return 1

    card.apply_review(review(card.review_state, quality))
    path = vault.save(card)
    state = card.review_state
    log.info(
        "%s → next review %s (+%s), ease %.2f, rep %d",
        path.name, state.next_review, humanise_interval(state.interval),
        state.ease_factor, state.repetition_count,
    )

    if config.git_auto_commit:
        from . import git_sync

        git_sync.commit_paths(
            [path],
            f"review({card.id}): quality={quality}",
            repo=config.vault_dir,
            push=not args.no_push,
        )
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deeprecall",
        description="Notion notes → decision-tree flashcards → spaced repetition on Telegram.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--sync", action="store_true", help="ingest Notion and generate cards")
    action.add_argument("--notify", action="store_true", help="push due cards to Telegram")
    action.add_argument("--bot-poll", action="store_true", help="serve the interactive bot")
    action.add_argument("--chat-id", action="store_true",
                        help="look up your Telegram chat id (run after messaging the bot)")
    action.add_argument("--stats", action="store_true", help="print vault statistics")
    action.add_argument("--review", metavar="CARD_ID", help="rate a card from the CLI")

    parser.add_argument("--quality", default="good", help="hard | good | easy (with --review)")
    parser.add_argument("--hours", type=int, default=None,
                        help="Notion lookback window (default INGEST_LOOKBACK_HOURS)")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap cards synthesised (--sync) or pushed (--notify)")
    parser.add_argument("--duration", type=int, default=None,
                        help="seconds to poll before exiting (--bot-poll)")
    parser.add_argument("--force", action="store_true",
                        help="re-synthesise notes whose card already exists")
    parser.add_argument("--dry-run", action="store_true",
                        help="with --sync, print ingested notes without calling the LLM")
    parser.add_argument("--no-push", action="store_true", help="commit locally but do not push")
    parser.add_argument("--vault", type=Path, default=None, help="override VAULT_DIR")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    try:
        config = Config.load()
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.vault:
        config.vault_dir = args.vault if args.vault.is_absolute() else REPO_ROOT / args.vault
    if args.no_push and args.sync:
        log.debug("Push disabled for this run.")

    try:
        if args.sync:
            return cmd_sync(config, args)
        if args.notify:
            return cmd_notify(config, args)
        if args.chat_id:
            return cmd_chat_id(config, args)
        if args.bot_poll:
            return cmd_bot_poll(config, args)
        if args.stats:
            return cmd_stats(config, args)
        if args.review:
            return cmd_review(config, args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.info("Interrupted.")
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard so CI logs a clean error
        log.error("Unexpected failure: %s", exc, exc_info=args.verbose)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
