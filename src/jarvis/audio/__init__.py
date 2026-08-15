"""Audio-path building blocks that sit below the agent.

Echo cancellation, the playback reference it needs, and the calibration that
lines the two up. Nothing here knows about transcripts, tools or replies —
it works on frames, before the listener has decided anything.
"""
