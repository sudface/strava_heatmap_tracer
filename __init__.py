"""QGIS entry point for Strava Heatmap Tracer."""


def classFactory(iface):
    from .plugin import StravaHeatmapTracer
    return StravaHeatmapTracer(iface)
