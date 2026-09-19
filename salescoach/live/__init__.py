"""Live capture: callcap frames -> crash-safe archive, level meters, VAD, live ASR.

Nothing in this package is allowed to make capture depend on transcription.
The archive is the product; the live transcript is a convenience that can
fail, fall behind, or be disabled without losing a second of audio.
"""
