# Controlled test media

`ultrasave-sample.png` and `ultrasave-sample.mp4` are synthetic assets generated
by `tools/generate_fixtures.py` for this project. The video is two seconds of a
test pattern and a 440 Hz tone (H.264 baseline / AAC). They contain no downloaded
social content. Both must be present for the mixed-collection provider.

Regenerate with `python tools/generate_fixtures.py` after installing
`requirements-dev.txt`. FFmpeg is a development-time generator dependency;
the P3 worker only copies these fixed, checked-in files.
