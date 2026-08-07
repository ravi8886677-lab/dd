"""Text chat session — the keyboard front door to the reply engine.

Same engine, memory, tools and diary as the voice path; the microphone,
Whisper, TTS and hotkey stacks are simply never imported, so this runs on
a headless box (server, container, SSH session) where the voice daemon
cannot start.
"""

from __future__ import annotations

import dataclasses
import sys
from typing import Optional, TextIO

from ..config import load_settings
from ..debug import debug_log
from ..llm import Tier, resolve_model
from ..memory.conversation import DialogueMemory, update_diary_from_dialogue_memory
from ..memory.db import Database
from ..memory.graph_ops import (
    install_warm_profile_invalidation,
    uninstall_warm_profile_invalidation,
)
from ..reply.engine import run_reply_engine
from ..tools.registry import discover_and_report_mcp_tools

# Everything typed here is stdin-sourced, so memory written from this
# front end is attributed to "stdin" (the voice path uses "voice").
SOURCE_APP = "stdin"

PROMPT = "🧑 You › "

EXIT_COMMAND = "/exit"
EXIT_COMMANDS = {EXIT_COMMAND, "/quit"}
HELP_COMMAND = "/help"
RESET_COMMAND = "/reset"


def _print_banner(cfg) -> None:
    print("", flush=True)
    print("💬 Jarvis text chat", flush=True)
    print(f"  🔌 Provider: {cfg.llm_provider} ({cfg.llm_base_url})", flush=True)
    print(f"  🧠 Chat model: {cfg.llm_chat_model}", flush=True)
    print(f"  ⚡ Fast model: {resolve_model(cfg, Tier.FAST)}", flush=True)
    print(f"  💾 Memory: {cfg.db_path}", flush=True)
    print(f"  ℹ️  Type {HELP_COMMAND} for commands, {EXIT_COMMAND} to leave.", flush=True)
    print("", flush=True)


def _print_help() -> None:
    print("", flush=True)
    print("📖 Commands", flush=True)
    print(f"  💬 {HELP_COMMAND}    Show this list", flush=True)
    print(f"  🧹 {RESET_COMMAND}   Save the conversation and start a fresh one", flush=True)
    print(f"  👋 {EXIT_COMMAND}    Save the conversation and quit (also /quit, or Ctrl+D)", flush=True)
    print("  ✍️  Anything else is sent to Jarvis.", flush=True)
    print("", flush=True)


def _flush_diary(db: Database, cfg, dialogue_memory: DialogueMemory, *, force: bool) -> None:
    """Write pending conversation to the diary and knowledge graph.

    Mirrors the voice daemon: a non-forced call is a cheap no-op unless
    the conversation has gone idle, while shutdown and ``/reset`` force
    the write so nothing said in the session is lost. A forced write can
    take a few seconds on a slow model, so it announces itself.
    """
    if force and dialogue_memory.has_pending_chunks():
        print("💾 Saving conversation to memory...", flush=True)

    try:
        update_diary_from_dialogue_memory(
            db=db,
            dialogue_memory=dialogue_memory,
            cfg=cfg,
            source_app=SOURCE_APP,
            voice_debug=getattr(cfg, "voice_debug", False),
            timeout_sec=cfg.llm_chat_timeout_sec,
            force=force,
            thinking=getattr(cfg, "llm_thinking_enabled", False),
            graph_picker_model=resolve_model(cfg, Tier.FAST),
        )
    except Exception as e:
        debug_log(f"diary flush failed: {e}", "chat")
        print(f"  ⚠️ Could not save the conversation to memory: {e}", flush=True)


def _read_line(stream: TextIO, prompt: str) -> Optional[str]:
    """Read one line, or ``None`` at end of input.

    On a real terminal this goes through ``input()`` so the user gets
    line editing and history; a piped or injected stream is read
    directly.
    """
    if stream is sys.stdin and stream.isatty():
        try:
            return input(prompt)
        except EOFError:
            return None

    print(prompt, end="", flush=True)
    line = stream.readline()
    if line == "":
        return None
    return line.rstrip("\n")


def _ask(db: Database, cfg, dialogue_memory: DialogueMemory, text: str) -> None:
    """Send one query to the reply engine.

    The engine prints the reply itself, so there is nothing to echo
    here. An engine failure is reported and the session continues — a
    bad turn should not throw away the conversation.
    """
    debug_log(f"chat query: '{text}'", "chat")
    try:
        run_reply_engine(db, cfg, None, text, dialogue_memory)
    except Exception as e:
        debug_log(f"reply engine exception: {e}", "chat")
        print(f"\n  ❌ Reply engine error: {e}\n", flush=True)


def run_chat_session(cfg, *, stdin: Optional[TextIO] = None, one_shot: Optional[str] = None) -> int:
    """Run a text chat session against ``cfg``.

    With ``one_shot`` set, answers that single query and returns without
    reading any input. Otherwise reads queries until ``/exit``, end of
    input, or Ctrl+C. Returns a process exit code.
    """
    stream = stdin if stdin is not None else sys.stdin

    db = Database(cfg.db_path, cfg.sqlite_vss_path)
    dialogue_memory = DialogueMemory(
        inactivity_timeout=cfg.dialogue_memory_timeout,
        max_interactions=20,
    )
    warm_profile_listener = install_warm_profile_invalidation(lambda: dialogue_memory)

    try:
        # Knowledge graph: wipe + re-seed if the on-disk shape predates
        # the User/Directives/World taxonomy. The diary is untouched.
        try:
            from ..memory.graph import GraphMemoryStore
            graph_store = GraphMemoryStore(cfg.db_path)
            if graph_store.migrate_legacy_shape():
                print("🧹 Wiped legacy knowledge graph; re-seeded User / Directives / World branches", flush=True)
                print("   📥 Open the memory viewer and use 'Import from Diary' to repopulate.", flush=True)
            graph_store.close()
        except Exception as e:
            debug_log(f"graph legacy-shape migration failed (non-fatal): {e}", "chat")

        discover_and_report_mcp_tools(getattr(cfg, "mcps", {}) or {})

        if one_shot is not None:
            _ask(db, cfg, dialogue_memory, one_shot)
            return 0

        _print_banner(cfg)

        while True:
            try:
                line = _read_line(stream, PROMPT)
            except KeyboardInterrupt:
                print("", flush=True)
                break

            if line is None:
                print("", flush=True)
                break

            text = line.strip()
            if not text:
                continue

            if text in EXIT_COMMANDS:
                break

            if text == HELP_COMMAND:
                _print_help()
                continue

            if text == RESET_COMMAND:
                _flush_diary(db, cfg, dialogue_memory, force=True)
                dialogue_memory.start_new_conversation()
                print("🧹 Started a fresh conversation.", flush=True)
                continue

            _ask(db, cfg, dialogue_memory, text)

            # Cheap idle check — a no-op mid-conversation, writes the
            # diary when the user has been away long enough.
            _flush_diary(db, cfg, dialogue_memory, force=False)

        return 0
    finally:
        _flush_diary(db, cfg, dialogue_memory, force=True)

        try:
            from ..tools.external.mcp_runtime import shutdown_runtime
            shutdown_runtime()
        except Exception as e:
            debug_log(f"MCP runtime shutdown error: {e}", "chat")

        uninstall_warm_profile_invalidation(warm_profile_listener)
        db.close()
        print("👋 Goodbye.", flush=True)


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``python -m jarvis.chat``.

    Any arguments are joined into a single one-shot query; with no
    arguments the session is interactive.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    one_shot = " ".join(args).strip() or None

    # This front end is stdin by definition, so memory written from it
    # is attributed accordingly.
    cfg = dataclasses.replace(load_settings(), use_stdin=True)

    try:
        return run_chat_session(cfg, one_shot=one_shot)
    except KeyboardInterrupt:
        return 0
