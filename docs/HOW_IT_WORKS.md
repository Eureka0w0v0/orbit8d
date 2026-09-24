# How Orbit 8D Works

This document explains what happens inside Orbit 8D, from the moment you drop a song in to the moment you download the 8D version. It is written for curious users and developers; no signal-processing background is assumed. For exact formulas and parameter ranges, see the engineering specification [`SPEC.md`](SPEC.md) (in Chinese).

**Contents**

1. [What "8D audio" is](#1-what-8d-audio-is)
2. [The big picture](#2-the-big-picture)
3. [From file to project: importing](#3-from-file-to-project-importing)
4. [Describing motion: scenes and the timeline](#4-describing-motion-scenes-and-the-timeline)
5. [Rendering a moving sound for two ears](#5-rendering-a-moving-sound-for-two-ears)
6. [Room reverb](#6-room-reverb)
7. [Keeping the original timbre](#7-keeping-the-original-timbre)
8. [Mastering](#8-mastering)
9. [The live preview in the browser](#9-the-live-preview-in-the-browser)
10. [How good is it?](#10-how-good-is-it)
11. [Safety and reliability](#11-safety-and-reliability)
12. [Testing](#12-testing)
13. [Where to find things in the code](#13-where-to-find-things-in-the-code)

---

## 1. What "8D audio" is

"8D" is a popular name for music that seems to move around your head. There is nothing eight-dimensional about it: the trick is **binaural rendering**. Your brain works out where a sound comes from using a few cues:

- **Timing** — a sound from the right reaches the right ear up to about 0.7 ms earlier than the left.
- **Level** — the head shadows the far ear, especially at high frequencies.
- **Ear shape** — the outer ear filters sound differently for sources in front, behind, above and below.

A **head-related transfer function (HRTF)** captures all three cues for every direction. Filtering a sound with the HRTF of a direction and playing it over headphones makes it appear to come from that direction; changing the direction over time makes it move. Loudspeakers mix both channels into both ears, which destroys the cues — hence "use headphones".

Most "8D" videos online just pan the whole mixed song left and right and add reverb. Orbit 8D goes further: it separates the song into instrument groups, gives each its own path around a measured human head, keeps the deep bass anchored in front, follows the song's structure, and keeps the original sound quality.

## 2. The big picture

```
┌──────────────────────────────── your computer ────────────────────────────────┐
│                                                                                 │
│   Browser  (http://127.0.0.1:8765)             Local server  (Python)           │
│   ─────────────────────────────                ─────────────────────            │
│   3D editor (Three.js)                         HTTP API (FastAPI)               │
│   timeline and panels          ◄── HTTP ──►    job queue (one job at a time)    │
│   real-time preview                            ffmpeg · Demucs                  │
│     (AudioWorklet, TypeScript)                 DSP engine (NumPy / SciPy)       │
│                                                                                 │
│                          data/   projects · exports · logs                      │
└─────────────────────────────────────────────────────────────────────────────────┘
```

- The **server** does the heavy work: decoding, stem separation and analysis (once per song) and the final high-quality export.
- The **browser** shows the 3D editor and plays a real-time preview using a TypeScript copy of the same renderer.
- Both sides use exactly the same data, all produced by the server: the HRTF table, the reverb impulse responses, the per-song calibration and tone correction, and your saved scene. Whatever you hear in the preview is what the export will contain.

## 3. From file to project: importing

### 3.1 Upload, identify, decode

1. The file is streamed to disk and hashed with **SHA-256**; the first 16 hex digits become the project id. Importing the same file again finds the existing project, so nothing is processed twice.
2. **ffprobe** checks the file against a whitelist of audio codecs (MP3, AAC, ALAC, FLAC, PCM, Vorbis, Opus) and the size and duration limits.
3. **ffmpeg** decodes it to 44.1 kHz stereo 32-bit float. Mono files are copied to both channels; surround files are downmixed.

### 3.2 Stem separation

[Demucs](https://github.com/facebookresearch/demucs) (the `htdemucs_ft` model) splits the song into four stems: **vocals, drums, bass, other**. It runs on the Apple GPU (MPS) when available, otherwise CUDA, otherwise the CPU. Separation is never perfect, so the leftover (original minus the sum of the stems) is added to "other" — nothing from the original is lost.

### 3.3 Analysis

The analysis runs once per song and takes about 10–15 seconds.

- **Sources.** Each stem is split at 120 Hz with a zero-phase crossover; the two halves add back to the original exactly. The part above 120 Hz moves; the deep bass below it stays in front, because rotating deep bass is uncomfortable and our ears barely localise it anyway. Vocals and bass become single (mono) sources; drums and "other" stay stereo and are rendered as a left and a right source.
- **Tempo.** The onset strength of the drum stem (how suddenly its spectrum rises) is scanned for the strongest periodicity between 60 and 180 BPM. The tempo is then folded into 70–140 BPM so that "one bar" always means a musically sensible length.
- **Default speed and alignment.** The default orbit speed is the whole number of bars per turn (1, 2, 4 or 8) closest to 7 seconds per turn. The reference time *t_ref* is placed on the strongest beat of the turn, so the orbits pass directly in front of you on a strong beat.
- **Calibration.** The song is rendered once with the *Classic 8D* preset. Spatialisation changes loudness in direction-dependent ways, so each track gets a gain that restores its original energy. The same gains are used in the preview and the export, so the balance between vocals, drums, bass and instruments stays as the artist mixed it.
- **Tone anchor.** The long-term spectrum of that same render is compared with the original in 27 third-octave bands (39 Hz – 16 kHz). The difference becomes the starting point of the tone correction (section 7).
- **Sections.** For every bar, the app measures the timbre (energy in 24 logarithmic frequency bands) and how loud the vocals are. Comparing every bar with every other bar gives a *self-similarity matrix*; sliding a checkerboard-shaped kernel along its diagonal gives a *novelty curve* that peaks where the music changes. The strongest peaks become section boundaries (on bar lines, each section at least 4 bars, at most 8 sections). Sections are then labelled by loudness and vocal presence: quiet sections without vocals at the start and end become intro and outro, the loudest middle sections become choruses, a quieter section between the second and third chorus becomes the bridge, and the rest are verses.
- **For the interface.** A loudness curve of the original (one value every 0.25 s) is drawn on the timeline, and a gain is computed that makes the original exactly as loud as the 8D preview, for fair A/B comparison.
- **Preview files.** The seven preview sources (4 moving parts, 3 deep-bass parts) and the original are written as 24-bit FLAC files for the browser.

A project moves through an explicit state machine — `UPLOADED → DECODING → SEPARATING → ANALYZING → READY` (or `FAILED`) — and the interface shows each stage. When a new version of Orbit 8D changes the analysis, existing projects are re-analysed once at startup (`READY → ANALYZING → READY`) without separating the stems again.

## 4. Describing motion: scenes and the timeline

### 4.1 The scene

Everything you edit is stored in a small JSON document called a **scene**:

```jsonc
{
  "version": 2,
  "mix": { "vocals": { "gain_db": 0, "width_deg": 0, "reverb_send": 1.0, "mute": false, "solo": false }, "...": {} },
  "sections": [
    { "start_s": 0,    "label": "前奏", "wet_db": -8,  "orbits": { "vocals": { "shape": "spiral", "height_deg": 35, "...": 0 }, "...": {} } },
    { "start_s": 25.9, "label": "主歌", "wet_db": -12, "orbits": { "...": {} } }
  ],
  "events": [ { "t_s": 47.5, "kind": "overhead", "duration_s": 3.6, "targets": ["vocals", "other"] } ],
  "room": { "name": "hall" },
  "rear_darken_db": 6
}
```

- **mix** — per-track volume, stereo width, reverb send, mute and solo, for the whole song.
- **sections** — from `start_s` to the next section's start, each track follows its own **orbit**; `wet_db` is that section's reverb level. (Section labels are stored as canonical Chinese names and translated in the interface.)
- **events** — *hold* (stop moving for a while) and *overhead* (fly over the top of the head).

The server validates every scene against a whitelist of fields and ranges before using or saving it.

### 4.2 Orbits

Directions use **azimuth** (0° in front, 90° to the right, clockwise seen from above) and **elevation** (up is positive). An orbit is defined in its own flat frame:

| Shape | Azimuth | Elevation | Distance |
|---|---|---|---|
| Circle | φ | height | R |
| Ellipse | atan2(sin φ, a·cos φ) | height | R·√(sin²φ + a²cos²φ) |
| Pendulum | swing·sin φ | height | R |
| Figure 8 | swing·sin φ | height + lift·sin 2φ | R |
| Spiral | φ | height + lift·sin(φ/4) | R |
| Fixed | start (constant) | height | R |

where φ is the **phase** (how far round the orbit the sound is). The whole orbit is then tilted by three rotations (front/back tilt, left/right tilt and turn), which is how "ear to ear", "front to back" and the diagonal orbits are made.

### 4.3 The timeline

Within one section, the phase simply grows steadily: φ = start + direction × 360° × (t − t_ref) / period. With several sections, events and speed changes, Orbit 8D computes the phase as the **integral of the angular speed**:

- The angular speed changes from section to section, and ramps down to zero and back up around a *hold* (half a second each way). Between these points it changes linearly, so the integral has an exact closed form — the phase is continuous everywhere, and the sound never jumps.
- **Section transitions.** Around each boundary (half a bar either side, or less if a section is short) the direction glides from the old orbit to the new one using *spherical interpolation* with a smooth S-curve, and the distance glides linearly.
- **Fly overhead.** During the event, the direction is pulled toward straight up with a weight that rises and falls like sin², reaching the top exactly in the middle of the event.
- **Section reverb** crossfades with the same timing as the motion.

With a single section and no events, the result is exactly the simple formula above.

### 4.4 Auto choreography

The auto choreography turns the detected sections into a scene: a slow raised spiral for the intro and outro (+4 dB reverb), classic 8D for verses, double speed on opposite 45° diagonals for choruses, a vertical 3D cross for the bridge (+2 dB reverb), a fly-over at the start of the loudest chorus and a one-bar vocal hold three quarters of the way through each long verse. It is only a starting point — every value remains editable.

## 5. Rendering a moving sound for two ears

### 5.1 The HRTF

Orbit 8D uses the TH Köln measurements of a **Neumann KU100** dummy head: 89 elevations × 180 azimuths (every 2°) over the whole sphere. At startup they are resampled to 44.1 kHz, trimmed to 128-sample filters (*HRIRs*), and normalised so that the average energy around the horizontal circle is 1.

### 5.2 Block by block

Each moving source is processed in blocks of **32 samples** (about 0.7 ms, so the direction is updated more than 1,300 times per second). For every block:

1. **Position** — the timeline gives azimuth, elevation and distance at the block's centre.
2. **Darken behind you** — a gentle high-frequency cut (a first-order high-pass at 3 kHz subtracted from the signal), scaled by how far behind the head the source is. It mimics the ear's natural shadowing and helps you tell front from back.
3. **Distance** — gain = 1 / distance (clamped to 0.5–4 m), so 1 m is the reference level.
4. **HRIR** — the filters of the four nearest measured directions are blended by bilinear interpolation.
5. **Convolution** — the block is convolved with the left- and right-ear HRIRs, and the tails that spill past the block are added into the following blocks (*overlap-add*).

Stereo tracks are two sources placed at ±width/2 along the same orbit. The deep bass below 120 Hz is rendered once from straight ahead, identically in both ears.

Why 32 samples? Updating the direction less often makes fast orbits "step" audibly. With 32-sample blocks the error compared with much finer blocks is about −54 dB, and 32 divides the browser's 128-sample audio quantum exactly, so the browser can process the same blocks as the server.

## 6. Room reverb

Dry binaural sound tends to sit "inside" the head. A little room sound fixes that:

- Each track sends part of its signal (volume × calibration × *Reverb send*) into a mono **reverb send**, which is multiplied block by block by the current section's reverb level.
- The send is convolved with a **binaural room impulse response (BRIR)**, synthesised at startup:
  - a **diffuse tail** from 16 horizontal directions, each with its own decorrelated noise, decaying at different rates in 5 frequency bands according to the room type, and passed through that direction's HRIR;
  - **8 early reflections** arriving 5–25 ms after the sound, from all around the head including above and below, which make the sound feel external and spacious;
  - a 150 Hz high-pass, so reverb never muddies the bass.

| Room | Decay time (low → high frequencies) | Pre-delay |
|---|---|---|
| Small room | 0.8 → 0.3 s | 8 ms |
| Hall | 2.2 → 0.6 s | 18 ms |
| Church | 4.0 → 0.9 s | 30 ms |

## 7. Keeping the original timbre

Every HRTF colours the sound, and the colouring depends on direction: sounds behind you are darker, sounds above are brighter, and the average over a whole orbit is not flat. Without correction, a classic 8D render of a song came out about 3.5–4 dB dull around 8 kHz.

Orbit 8D corrects this in two steps:

1. **Measured anchor.** During analysis, the classic render is compared with the original song (section 3.3). This measurement includes everything — HRTFs, rear darkening, reverb, and how the left and right halves of a stereo track add up at each ear.
2. **Predicted change for your scene.** For the scene you are actually using, a model predicts its colouring: for each source it follows the orbit (four points per second), looks up the HRTF power in each frequency band, applies the rear darkening and distance, weights it by that source's own spectrum and adds the reverb. The correction is the anchor plus the difference between the classic scene's colouring and yours.

The result is turned into a 1,025-tap linear-phase filter. The preview asks the server for the filter of the current scene a quarter of a second after each edit and crossfades to it; the export uses the same function. Your own mix decisions (volume, mute, solo) are not "corrected back", because the colouring is computed relative to the same mix without spatialisation.

## 8. Mastering

The export aims for −9 LUFS, but the gain is capped so that at most 1% of the song's 30 ms segments need more than 1 dB of limiting. A look-ahead **true-peak limiter** (4× oversampled) then keeps every peak below −1 dBTP. In practice exports land around −12 LUFS: a little quieter than loud commercial masters, but with the song's dynamics intact.

## 9. The live preview in the browser

```
9-channel buffer ──► AudioWorklet ──► binaural signal ─────────────────┐
(vocals, bass,         (TypeScript     reverb send ──► BRIR convolver ─┤
 drums L/R, other L/R,  renderer)                                       ▼
 3 deep-bass parts)                          tone-correction EQ (two convolvers, crossfaded)
                                                                         ▼
original (for A/B) ─────────────────────────────► preview gain ──► safety limiter ──► output
```

- The worklet runs the TypeScript twin of the server's timeline and block renderer on every 128-sample audio quantum (four 32-sample blocks).
- The timeline is compiled into plain arrays whenever the scene changes and sent to the worklet, so no memory is allocated while audio is playing.
- The original song plays in perfect sync on a separate path; pressing **B** crossfades between the two paths in 40 ms.
- The 3D view moves the dots using the same timeline code, so what you see matches what you hear.

## 10. How good is it?

Measured on two songs (Faded, Die For You) with the same metrics for every version; the reference is a popular 8D video.

| Metric | Before the v2 audio work | Classic 8D now | Auto choreography | Original song | Reference 8D video |
|---|---|---|---|---|---|
| Largest timbre deviation from the original | −3.9 / −3.4 dB (at 8 kHz) | −0.4 / −0.4 dB | −1.3 / −1.6 dB | — | — |
| Interaural correlation (lower = more spacious) | 0.83 / 0.88 | 0.80 / 0.85 | 0.78 / 0.84 | 0.60 / 0.66 | 0.75 |
| Loudness range (LRA) | 5.5 / 4.9 LU | 6.6 / 5.7 LU | 6.2 / 5.7 LU | 7.1 / 5.5 LU | 7.9 LU |
| Interaural time difference at the sides | 0.68–0.70 ms | same | 0.57–0.63 ms | — | 0.68 ms |

All other presets stay within ±1.4 dB of the original's timbre. The correlation cannot reach the reference video's value because the deep bass is deliberately kept identical in both ears (for comfort); in the mid frequencies it improved from 0.78 to 0.74.

## 11. Safety and reliability

- **Local only.** The server binds to `127.0.0.1` with no option to change it, accepts only `127.0.0.1`/`localhost` as the Host header (against DNS rebinding) and rejects cross-site write requests by checking the `Origin` header.
- **Strict input.** Uploads are size- and duration-limited and checked against a codec whitelist; ffmpeg is always called with argument lists, never through a shell; file names from users are only used for display.
- **Validated data.** Scenes are validated against explicit ranges; invalid ones are rejected and never overwrite a saved scene.
- **Content-addressed and idempotent.** Project ids come from the file content; export ids come from the song, the scene, the format and the renderer version, so repeating an export returns the finished file instead of rendering again.
- **Explicit state machines** for projects, exports and the interface: illegal transitions raise errors instead of silently misbehaving.
- **Crash-safe files.** Records and scenes are written to a temporary file and atomically renamed. Jobs interrupted by a shutdown are marked as failed on the next start and can simply be retried.
- **Structured logs.** Every job logs JSON lines with a trace id (`data/logs/orbit8d.log`).

## 12. Testing

- **Python:** about 200 tests covering the orbit formulas, the timeline, HRTF handling, the block renderer, reverb, mastering, tone correction, tempo and section detection, the scene model and its upgrade path, the state machines and the HTTP API (including security boundaries).
- **TypeScript:** tests for the timeline, the renderer, scene editing, undo/redo, the orientation controls and the three-language dictionaries.
- **Golden data** in `shared/golden/`, generated by Python and checked by TypeScript, guarantees that both implementations agree: orbit and timeline positions within 10⁻⁹, rendered audio within −90 dB.

Run everything with `make test`.

## 13. Where to find things in the code

| Topic | Server (Python) | Browser (TypeScript) |
|---|---|---|
| Orbit shapes and rotations | `backend/orbit8d/engine/orbit.py` | `web/src/orbit/orbit.ts` |
| Timeline (phase, transitions, events) | `engine/timeline.py` | `web/src/orbit/timeline.ts` |
| HRTF loading and interpolation | `engine/hrtf.py` | `web/src/audio/hrtf.ts` |
| Block renderer | `engine/render.py`, `engine/pipeline.py` | `web/src/audio/core.ts`, `worklet.ts` |
| Reverb | `engine/reverb.py` | (impulse response served by the backend) |
| Tone correction | `engine/tone.py`, `engine/master.py` | `web/src/audio/engine.ts` (applies it) |
| Mastering | `engine/master.py` | — |
| Tempo, sections, choreography | `engine/tempo.py`, `engine/structure.py`, `engine/choreo.py` | — |
| Scene model | `engine/scene.py` | `web/src/types.ts`, `web/src/scene/edit.ts` |
| Jobs and state machines | `jobs/` | `web/src/app.ts` (interface phases) |
| HTTP API | `api/app.py` | `web/src/api.ts` |
| Interface text | — | `web/src/i18n/` |
