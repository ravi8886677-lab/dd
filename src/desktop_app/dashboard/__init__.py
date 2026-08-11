"""Static browser frontend for the local Jarvis dashboard.

This package deliberately imports neither the Flask server nor Qt. Keeping
the assets independent lets web tooling inspect them without initialising a
desktop application or display connection.
"""
