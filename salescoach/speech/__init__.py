"""Speech: model registry, final-tier batch transcription, diarization.

Models are never downloaded implicitly. Everything that needs a model checks
the local cache first and fails with the exact command that fetches it.
"""
