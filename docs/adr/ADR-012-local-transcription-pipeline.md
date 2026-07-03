> **Vendored copy.** Canonical source: the YT Transcript Copier
> extension repo. When editing, prefer the canonical source and
> re-vendor this file afterward (update the `Vendored on:` date).
>
> Vendored on: 2026-07-03

# ADR-012: Local Transcription Pipeline

| Field    | Value      |
| -------- | ---------- |
| Status   | Proposed   |
| Date     | 2026-07-03 |
| Author   | Mavis      |
| Deciders | TBD        |

## Context

The Chrome extension enables users to obtain transcripts of YouTube videos quickly.

However, a non-trivial portion of videos either lack accessible subtitles, or the existing subtitle extraction mechanism fails for them.

An alternative path for obtaining the transcript is required, without relying on cloud services.

A persistently available local compute node is available to perform speech-to-text inference.

> **Current deployment:** Mac Mini.

## Assumptions

- A local compute node is available.
- The compute node can reach the media source.
- The extension can communicate with the local API.

## Decision Drivers

- **Local-first.** Inference must not depend on cloud APIs.
- **Minimal browser logic.** The extension remains a thin client.
- **Reusable processing pipeline.** The STT engine is replaceable without re-architecting the system.
- **No cloud inference.** Privacy and operational simplicity.
- **Low operational complexity.** Minimal moving parts, no distributed state.

## Decision

Add a new context menu entry to the extension: **"Transcribe locally"**.

When the user selects this command:

1. The extension sends the current video URL to the local API on the compute node.
2. The compute node places the job in a queue.
3. A processing pipeline executes three sequential stages:
   - media acquisition
   - audio conditioning
   - speech-to-text inference
4. The result is stored and exposed through the local API.
5. The client periodically polls the server until the job reaches a terminal state.
6. Upon completion, the transcript is presented to the user.

Speech-to-text inference is performed entirely on the local compute node.

### Architecture

```
Chrome Extension
      │
      ▼
  Local API
      │
      ▼
    Queue
      │
      ▼
Processing Pipeline
      │
      ▼
Speech-to-Text Engine
      │
      ▼
Transcript Result
```

## Considered Alternatives

### Alternative A: Cloud Speech-to-Text

Use a managed STT API (e.g., Whisper API, Deepgram, AssemblyAI).

**Rejected.** Violates the _Local-first_ and _No cloud inference_ drivers. Introduces recurring cost, external availability dependency, and a data-egress boundary.

### Alternative B: Browser-side STT via WebAssembly

Run a speech-to-text model directly in the browser through a WebAssembly port.

**Rejected.** Model size and per-tab memory footprint are impractical for general use; contradicts _Minimal browser logic_.

### Alternative C: Local Compute Node _(chosen)_

A persistently available local node executes the full pipeline; the extension acts as a thin client.

**Accepted.** Satisfies all five decision drivers. Hardware-agnostic: the same architecture applies whether the node is a Mac Mini, a Linux mini-PC, or a Jetson.

### Alternative D: YouTube built-in captions

Rely on the existing subtitle extraction mechanism already implemented in the extension.

**Rejected as primary path.** Does not satisfy the Context requirement: a non-trivial portion of videos lack usable subtitles. Remains a useful fallback, but not a substitute for local transcription.

The selected architecture is the only alternative satisfying all Decision Drivers.

## Consequences

### Positive

- The extension remains a thin client with no heavy dependencies.
- No external API contracts, billing, or rate limits.
- Speech-to-text inference stays entirely on the local compute node.
- Individual pipeline stages remain replaceable without affecting the extension API.
- The architectural decision survives changes in the underlying model and in the hardware.

### Negative

- Transcription is not instantaneous.
- Requires a persistently available local compute node.
- Processing duration scales with input length.

## Out of Scope

The following are explicitly excluded from this decision and will require separate ADRs if pursued:

- Translation of transcripts to other languages.
- Speaker diarization.
- Multiple concurrent processing workers.
- Remote execution on nodes outside the local network.
- Persistent transcript retention policies.
- Cloud-based speech-to-text services.
