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
finishes. Heat is displayed as orange lines with a screen blend mode. **Load
cached tiles only** builds and loads that layer from tiles already on disk,
without downloading or tracing. Each VRT references its own neighbouring
`*_tiles` folder, so later runs do not change an earlier raster.

The GPX is copied into a QGIS **temporary memory layer**, which is independent
of the GPX file. If the chosen GPX output filename already exists, a timestamp
and short unique suffix are added so existing files remain intact. Temporary
QGIS vector layers are not persistent across QGIS restarts; export any you want
to keep. Keep a VRT and its `*_tiles` folder together if moving the raster.

The output is an inferred centerline network from an aggregate image. It
includes roads as well as paths and may include off-trail use or GPS errors.
Review individual candidates before using them as trails or editing OSM.
