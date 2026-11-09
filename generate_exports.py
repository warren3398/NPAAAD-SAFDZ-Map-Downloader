"""Run offline on a machine with sufficient memory; never on the Render web service.
Example: python generate_exports.py --island luzon --province abra --layer npaaad --class-field CLASS
Requires R2_* environment variables. Uploads only completed outputs.
"""
import argparse,os,tempfile,zipfile
from pathlib import Path
import boto3,geopandas as gpd
from botocore.config import Config
from map_layout import render_map
p=argparse.ArgumentParser()
for name in ("island","province","layer"):p.add_argument("--"+name,required=True)
p.add_argument("--class-field",help="Explicit source attribute containing classification codes")
p.add_argument("--skip-shapefile",action="store_true")
a=p.parse_args()
if a.island not in ("luzon","visayas","mindanao") or a.layer not in ("npaaad","safdz") or not a.province.replace("-","").isalnum():p.error("Invalid dataset")
account=os.environ["R2_ACCOUNT_ID"]
bucket=os.getenv("R2_BUCKET","npaaad-safdz-data")
s3=boto3.client("s3",endpoint_url=f"https://{account}.r2.cloudflarestorage.com",region_name="auto",aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],config=Config(signature_version="s3v4"))
prefix=f"{a.island}/{a.province}/{a.layer}"
with tempfile.TemporaryDirectory() as directory:
 root=Path(directory);source=root/"source.gpkg"
 s3.download_file(bucket,f"nationwide/{prefix}.gpkg",str(source))
 jpg=root/"map.jpg";pdf=root/"map.pdf"
 result=render_map(source,a.layer,a.province,jpg,pdf,class_field=a.class_field)
 print("Validated map:",result,flush=True)
 files={"jpg":jpg,"pdf":pdf}
 if not a.skip_shapefile:
  frame=gpd.read_file(source)
  shape=root/"shape";shape.mkdir()
  frame.to_file(shape/"dataset.shp",driver="ESRI Shapefile",encoding="UTF-8")
  archive=root/"dataset.zip"
  with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
   for path in shape.iterdir():z.write(path,path.name)
  files["zip"]=archive
 for ext,path in files.items():
  s3.upload_file(str(path),bucket,f"exports/{prefix}.{ext}",ExtraArgs={"ContentType":{"jpg":"image/jpeg","pdf":"application/pdf","zip":"application/zip"}[ext]})
  print("Uploaded:",f"exports/{prefix}.{ext}",flush=True)
