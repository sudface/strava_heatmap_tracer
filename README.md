# Strava Heatmap Tracer for QGIS

Install the ZIP through **Plugins → Manage and Install Plugins → Install from ZIP**.
Open **Plugins → Strava Heatmap Tracer → Trace Strava heatmap…**.

The plugin runs a bundled copy of `strava_heatmap_to_gpx.py` in a separate
Python process so that QGIS remains responsive. Select the Python executable
where `numpy`, `scipy`, and `pillow` are installed. If needed, install them in
that interpreter with `python -m pip install numpy scipy pillow`.

The dialog exposes the four latitude/longitude bounds, a button to take the
current QGIS map extent, cookie header value, tile zoom, brightness threshold,
signal channel, minimum segment length, simplification, download workers,
cache folder, output GPX and optional preview. The cookie is passed in the
child process environment for that run; the plugin does not save it.

Check **Add the cached heatmap as a transparent raster layer** to create a
georeferenced VRT alongside the GPX and load it into QGIS when extraction
finishes. **Load cached tiles only** builds and loads that layer from tiles
already on disk, without downloading or tracing. Keep the tile cache folder in
place: the VRT references PNGs generated inside its `qgis_tiles` subfolder.

The output is an inferred centerline network from an aggregate image. It
includes roads as well as paths and may include off-trail use or GPS errors.
Review individual candidates before using them as trails or editing OSM.
