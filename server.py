from flask import Flask, request, send_file, Response
from flask_cors import CORS
import os
import json
import requests
import rasterio
import geopandas as gpd
import numpy as np
from rasterio.mask import mask
from rasterio.warp import transform_geom
from rasterio.windows import Window
from io import BytesIO
import logging
from functools import lru_cache
from datetime import datetime, timedelta
from flask_compress import Compress

app = Flask(__name__)
# Explicitly configure CORS for all endpoints
CORS(app, resources={
    r"/api/*": {
        "origins": ["https://proba-v-fapar.onrender.com", "http://localhost:8000"],  # Allow frontend origins
        "methods": ["GET", "POST", "HEAD"],  # Allow POST for clip/process
        "allow_headers": ["Content-Type"]  # Allow headers used in POST
    },
    r"/health": {"origins": "*"},  # Allow health check from any origin
    r"/": {"origins": "*"}  # Allow root from any origin
})
Compress(app)  # Enable Gzip compression
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# In-memory cache for VITO directory listings
directory_cache = {}

def cache_directory(url, data):
    directory_cache[url] = {'data': data, 'timestamp': datetime.now()}

def get_cached_directory(url, ttl_hours=24):
    if url in directory_cache:
        entry = directory_cache[url]
        if datetime.now() - entry['timestamp'] < timedelta(hours=ttl_hours):
            return entry['data']
    return None

@lru_cache(maxsize=100)
def fetch_vito_directory(url):
    cached = get_cached_directory(url)
    if cached:
        return cached
    try:
        r = requests.get(url, stream=True, timeout=10)
        if r.status_code != 200:
            raise Exception(f"Failed to fetch {url}")
        data = r.text
        cache_directory(url, data)
        return data
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

@app.route('/', methods=['GET'])
def index():
    return Response("FAPAR Flask Server is running.", status=200, mimetype='text/plain')

@app.route('/health', methods=['GET'])
def health():
    return Response("OK", status=200, mimetype='text/plain')

@app.route('/api/clip-raster', methods=['POST'])
def clip_raster():
    try:
        if 'shapefile' not in request.form or 'tiffUrl' not in request.form:
            return {"error": "Missing shapefile or tiffUrl"}, 400

        shapefile_data = json.loads(request.form['shapefile'])
        tiff_url = request.form['tiffUrl']
        if not tiff_url.endswith('.tiff'):
            return {"error": "Invalid TIFF URL"}, 400

        gdf = gpd.GeoDataFrame.from_features(shapefile_data['features'], crs='EPSG:4326')
        if gdf.empty:
            return {"error": "Empty shapefile"}, 400
        geometry = gdf.geometry.values[0]

        # Stream TIFF download
        local_path = 'temp.tif'
        r = requests.get(tiff_url, stream=True, timeout=30)
        if r.status_code != 200:
            return {"error": f"Failed to download TIFF: {tiff_url}"}, 404
        with open(local_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        # Windowed reading
        with rasterio.open(local_path) as src:
            nodata = src.nodata if src.nodata is not None else 255
            geom_in_raster_crs = transform_geom(gdf.crs, src.crs, geometry.__geo_interface__)
            window = src.window(*gdf.to_crs(src.crs).total_bounds)
            window = window.intersection(Window(0, 0, src.width, src.height))
            out_image = src.read(window=window)
            out_transform = src.window_transform(window)
            out_image = np.where(out_image > 236, nodata, out_image)

            out_meta = src.meta.copy()
            out_meta.update({
                "driver": "GTiff",
                "height": out_image.shape[1],
                "width": out_image.shape[2],
                "transform": out_transform,
                "nodata": nodata,
                "compress": "LZW"
            })

        output = BytesIO()
        with rasterio.open(output, 'w', **out_meta) as dst:
            dst.write(out_image)
        output.seek(0)
        os.remove(local_path)
        return send_file(output, mimetype='image/tiff', as_attachment=True, download_name='clipped.tif')
    except Exception as e:
        logger.error(f"Error clipping raster: {e}")
        return {"error": str(e)}, 500

@app.route('/api/process-raster', methods=['POST'])
def process_raster():
    try:
        if 'shapefile' not in request.form or 'tiffs' not in request.form or 'compositeType' not in request.form:
            return {"error": "Missing required fields"}, 400

        shapefile_data = json.loads(request.form['shapefile'])
        tiffs = json.loads(request.form['tiffs'])
        composite_type = request.form['compositeType']
        if not tiffs or not isinstance(tiffs, list):
            return {"error": "Invalid TIFF list"}, 400
        if composite_type not in ['max', 'min', 'mean', 'qualitymosaic']:
            return {"error": "Invalid composite type"}, 400

        gdf = gpd.GeoDataFrame.from_features(shapefile_data['features'], crs='EPSG:4326')
        if gdf.empty:
            return {"error": "Empty shapefile"}, 400
        geometry = gdf.geometry.values[0]

        images = []
        qflag_images = []
        meta = None
        temp_files = []

        for i, tiff_url in enumerate(tiffs[:5]):  # Limit to 5 TIFFs
            if not tiff_url.endswith('.tiff'):
                logger.warning(f"Skipping invalid TIFF URL: {tiff_url}")
                continue
            local_path = f'temp_{i}.tif'
            temp_files.append(local_path)
            r = requests.get(tiff_url, stream=True, timeout=30)
            if r.status_code != 200:
                logger.warning(f"Failed to download TIFF: {tiff_url}")
                continue
            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            try:
                with rasterio.open(local_path) as src:
                    nodata = src.nodata if src.nodata is not None else 255
                    window = src.window(*gdf.to_crs(src.crs).total_bounds)
                    window = window.intersection(Window(0, 0, src.width, src.height))
                    out_image = src.read(window=window)
                    out_transform = src.window_transform(window)
                    out_image = np.where(out_image > 236, nodata, out_image)
                    if 'QFLAG' in tiff_url:
                        qflag_images.append(out_image)
                    else:
                        images.append(out_image)
                    if meta is None:
                        meta = src.meta.copy()
                        meta.update({
                            "driver": "GTiff",
                            "height": out_image.shape[1],
                            "width": out_image.shape[2],
                            "transform": out_transform,
                            "nodata": nodata,
                            "compress": "LZW"
                        })
            except Exception as e:
                logger.error(f"Error processing TIFF {tiff_url}: {e}")
                continue

        if not images:
            for temp_file in temp_files:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            return {"error": "No valid FAPAR images processed"}, 400

        if meta is None:
            for temp_file in temp_files:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            return {"error": "Failed to initialize raster metadata"}, 500

        nodata = meta.get('nodata', 255)
        if composite_type == 'qualitymosaic':
            if qflag_images:
                qflag = qflag_images[0]
                masked_images = [np.where(img == nodata, -9999, img) for img in images]
                indices = np.argmax([qflag] * len(images), axis=0)
                result = np.choose(indices, masked_images)
                result = np.where(result == -9999, nodata, result).astype(meta['dtype'])
            else:
                result = images[0]  # Fallback to first image
        else:
            masked_images = [np.where(img == nodata, np.nan, img) for img in images]
            if composite_type == 'max':
                result = np.nanmax(masked_images, axis=0)
            elif composite_type == 'min':
                result = np.nanmin(masked_images, axis=0)
            elif composite_type == 'mean':
                result = np.nanmean(masked_images, axis=0)
            result = np.where(np.isnan(result), nodata, result).astype(meta['dtype'])

        output = BytesIO()
        with rasterio.open(output, 'w', **meta) as dst:
            dst.write(result)
        output.seek(0)

        for temp_file in temp_files:
            if os.path.exists(temp_file):
                os.remove(temp_file)

        return send_file(output, mimetype='image/tiff', as_attachment=True, download_name=f'composite_{composite_type}.tif')
    except Exception as e:
        logger.error(f"Error processing raster: {e}")
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        return {"error": str(e)}, 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))  # Use Render's PORT or default
    app.run(host='0.0.0.0', port=port, debug=False)  # Bind to 0.0.0.0
