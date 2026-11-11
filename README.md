# NPAAAD–SAFDZ Map Downloader

Select an island group and region, optionally a province, municipality, or barangay, then download JPEG, Shapefile ZIP, or GeoPackage. Leaving Province blank selects the whole region. There is no preview or separate generation step.

Downloads read the original NPAAAD / SAFDZ GeoPackages in Cloudflare R2. Province GeoPackage downloads stream the original file. Smaller areas use their administrative boundary to read intersecting source features and clip them. Region exports combine the region's source provinces; incomplete regional coverage returns an explicit error rather than a partial download. Aliases and region membership are in `administrative_catalog.json`; ambiguous `north` sources are excluded.

JPEG uses a simple title, classification legend, and north arrow. GIS exports retain the selected geometry; JPEG simplification affects display only. Shapefile ZIP includes `.shp`, `.shx`, `.dbf`, `.prj`, `.cpg`, and a field-name mapping. Cached selections can be reused across formats. The cache is local and ephemeral, and its identity includes source object versions.

## Configuration

Set `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and optionally `R2_BUCKET` (default `npaaad-safdz-data`). Boundary storage uses `BOUNDARY_R2_ACCOUNT_ID` (defaults to the source account), `BOUNDARY_R2_ACCESS_KEY_ID`, `BOUNDARY_R2_SECRET_ACCESS_KEY`, and `BOUNDARY_R2_BUCKET` (default `slopemap-gis-data`). Boundary keys are `boundaries/municipalities.gpkg` and `boundaries/barangays.gpkg` with `NAME_1`, `NAME_2`, and `NAME_3` for barangays.

Render build: `pip install -r requirements.txt`

Render start: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 2 --timeout 300`

Direct exports use the web process and serialize preparation to limit concurrent memory use. Large regional files require enough RAM, temporary disk, and request time; moving the button into a direct flow does not eliminate GIS processing. First-time downloads can take longer. Existing GitHub queue endpoints and automation remain available for compatibility but are not used by the downloader UI. There is no claim that every regional download fits a free web instance.

Tests: install `pytest`, then run `pytest -q tests`.

Regional JPEGs use a separate raster path: only classification and geometry are read in bounded batches, then each province is drawn at image resolution. The JPEG path never writes or reopens a merged full-detail GeoPackage. Province rasters are reused locally and, when storage permissions allow, in `exports/regional-rasters/` in the export bucket. Cache keys include the source key, ETag, modification time, style version, and resolution; source updates invalidate the cache. GIS downloads retain full geometry. The first uncached request still needs to read the original province datasets.

All JPEG levels (region, province, municipality, barangay) use the bounded raster renderer. Municipal and barangay rasters are clipped to their selected administrative boundary and cached by boundary geometry and source version. Display simplification is limited to subpixel detail. Vector exports read and clip 128 features per batch, append to a staged GeoPackage on disk, and stream Shapefile conversion in batches. Numeric attributes and source feature IDs are retained across batches; fields missing from a province are null. JPEG downloads now handle HTTP and connection errors directly instead of waiting for a cookie timeout. Province-wide original GeoPackage downloads continue streaming directly from source storage.
