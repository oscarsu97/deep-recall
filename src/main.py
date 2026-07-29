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


def cmd_sync(config: Config, args: argparse.Namespace) -> int:
    """Ingest recent Notion notes and synthesise them into vault cards."""
    from .ingestion import IngestionError, NotionIngestor
    from .synthesizer import SynthesisError, Synthesizer

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

    known = set() if args.force else vault.known_ids()
    pending = []
    for note in notes:
        if note.suggested_id in known:
            log.info("• skip (already in vault): %s", note.question[:70])
            continue
        pending.append(note)

    if args.limit:
        pending = pending[: args.limit]

    log.info("%d note(s) to synthesise (%d ingested)", len(pending), len(notes))

    if args.dry_run:
        for note in pending:
            print(f"\n--- {note.suggested_id} [{note.suggested_topic}]")
            print(f"Q: {note.question}")
            print(note.raw_answer[:500])
        return 0

    if not pending:
        return 0

    synthesizer = Synthesizer(config)
    written: list[Path] = []
    failures = 0

    for i, note in enumerate(pending, 1):
        log.info("[%d/%d] synthesising: %s", i, len(pending), note.question[:70])
        try:
            card = synthesizer.synthesize(note)
        except SynthesisError as exc:
            # One bad note must not abandon the rest of the batch.
            log.error("  ✗ %s", exc)
            failures += 1
            continue
        try:
            path = vault.save(card)
        except VaultError as exc:
            log.error("  ✗ could not write card: %s", exc)
            failures += 1
            continue
        log.info("  ✓ %s", _display(path))
        written.append(path)

    if written and config.git_auto_commit:
        from . import git_sync

        git_sync.commit_paths(
            written,
            f"sync: add {len(written)} card(s) from Notion ({date.today().isoformat()})",
            repo=config.vault_dir,
            push=not args.no_push,
        )

    log.info("Sync complete: %d card(s) written, %d failure(s)", len(written), failures)
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
    due = vault.due_cards()

    print(f"\n📊 DeepRecall vault — {config.vault_dir}")
    print(f"   cards         {stats['total']}  across {stats['topics']} topic(s)")
    print(f"   due today     {stats['due']}")
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
