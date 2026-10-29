# Print plates (generated)

Output folder for print plates: `tools/prusaslicer/plate.py` (or the `prusaslicer` MCP `arrange`) writes a `.3mf` with the parts
turned onto their print face and packed on the bed, plus a `.png` top view; slicing adds `.gcode`.

Everything here is generated on demand from `../*.stl` and `../plate.json`, so it is not committed (only this
README is, to keep the folder). Rebuild after every STL re-export — an old plate holds old geometry.
