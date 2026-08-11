# Realtime voice

A hosted speech-to-speech model holds the conversation; Jarvis does the work.
The model listens, decides when the user has finished a turn, and speaks. Every
capability behind that conversation (tools, memory, planning) stays in Jarvis
and is reached through function calls.

## Why this exists

The local pipeline pays a fixed cost before any model runs. `endpoint_silence_ms`
has to elapse in silence before Jarvis accepts that the user stopped talking,
and STT, the gate contexts, the reply loop and TTS all queue behind it. Tuning
the models does not remove that floor.

A realtime model replaces the silence timer with semantic turn detection: it
predicts the end of a turn from meaning, streams audio both ways over one
connection, and handles interruption natively. That is the entire reason for
this subsystem. It buys turn-taking latency, nothing else.

## Two brains, one boundary

The realtime model owns **conversation**: listening, endpointing, speaking,
interruption, prosody.

Jarvis owns **capability**: the tool registry, the MCP layer, the memory graph,
the planner.

The function-call bridge is the only connection between them, and it runs in one
direction. The realtime model may ask Jarvis to do something; it never reaches
into Jarvis's state directly. A capability Jarvis does not expose as a tool is
not reachable from a realtime session.

## The security gate still applies

Tool calls arriving from a realtime session run through the same path as tool
calls from the local reply loop, including the MCP confirmation gate and the
trust store. A session is a new way to *ask*, not a new authority.

This matters more here than elsewhere: the caller is a hosted model acting on
speech it heard in a room. Treating its requests as pre-authorised would let
anything audible in that room drive a tool call. Withheld tools stay withheld,
gated tools stay gated, and a tool absent from the discovery cache cannot be
invoked by name.

## Nothing said is lost

A realtime conversation reaches the memory pipeline the same way a local voice
turn does. Transcripts flow back into the diary and the graph, so switching
front ends does not create a class of conversation Jarvis cannot remember. A
session that ends without its transcript reaching memory is a bug, not a
trade-off.

## Falling back

The local pipeline is the default and the fallback. Realtime is off unless the
user turns it on and supplies credentials. Any failure (absent credentials, a
refused connection, a dropped socket, a provider error) returns Jarvis to the
local pipeline for that turn rather than failing the turn.

The user is told which path served them when it changes, because the two sound
different and silent degradation reads as a fault.

## Audio boundaries

Audio leaves the machine only while a session is active. Wake-word detection,
dictation and the idle listener stay local, always: a realtime session is
entered deliberately and is not a standing connection waiting on a wake word.

Ending a session closes the socket. There is no keep-alive holding an open
microphone stream against a remote endpoint.

## Coordinating with the local audio stack

One process, one set of speakers, one microphone. A realtime session takes the
audio lock for its lifetime, and the local TTS engine is stopped and drained
before the session speaks. Barge-in is handled by the model, so when it reports
that the user interrupted, local playback stops immediately rather than finishing
its current sentence.

## Provider independence

`RealtimeVoiceBackend` is the contract; a provider is an adapter behind it. The
session runner, the tool bridge and the audio coordination know nothing about
any vendor's wire format. Adding a provider means writing an adapter, not
touching the session.

## Threading

A realtime session is a long-lived socket in a codebase that is otherwise
synchronous. It follows the pattern `tools/external/mcp_runtime.py` already
establishes: one background thread owning an asyncio event loop, with a
synchronous API in front of it. Callers never see a coroutine.
