import os, json, sqlite3, tempfile, pathlib
import boto3
import pyogrio
import shapely
from collections import Counter

bucket=os.environ.get("R2_BUCKET_NAME","npaaad-safdz-data")
s3=boto3.client("s3",endpoint_url="https://"+os.environ["R2_ACCOUNT_ID"]+".r2.cloudflarestorage.com",aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],region_name="auto")
out=pathlib.Path("qa-results.jsonl")
with out.open("w") as report:
    for prefix in ("nationwide/","provisional/"):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket,Prefix=prefix):
            for obj in page.get("Contents",[]):
                key=obj["Key"]
                if not key.lower().endswith(".gpkg"): continue
                result={"key":key,"size":obj["Size"],"etag":obj["ETag"],"layers":[]}
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        path=pathlib.Path(tmp)/"data.gpkg"
                        s3.download_file(bucket,key,str(path))
                        if path.stat().st_size!=obj["Size"]: raise ValueError("size mismatch")
                        with sqlite3.connect(path.as_uri()+"?mode=ro",uri=True) as db:
                            result["integrity"]=[r[0] for r in db.execute("PRAGMA integrity_check")]
                        for name,kind in pyogrio.list_layers(path):
                            if kind is None: continue
                            info=pyogrio.read_info(path,layer=name)
                            stats=Counter()
                            with pyogrio.open_arrow(path,layer=name,columns=[],batch_size=8192,use_pyarrow=True) as source:
                                meta,reader=source
                                col=meta.get("geometry_name") or "wkb_geometry"
                                for batch in reader:
                                    g=shapely.from_wkb(batch.column(batch.schema.get_field_index(col)).to_pylist(),on_invalid="ignore")
                                    missing=shapely.is_missing(g)
                                    empty=shapely.is_empty(g)
                                    valid=shapely.is_valid(g)
                                    stats.update(total=len(g),missing_or_unreadable=int(missing.sum()),empty=int(empty.sum()),invalid=int((~valid & ~missing).sum()))
                            result["layers"].append({"name":name,"crs":info.get("crs"),"bounds":info.get("total_bounds"),**stats})
                        result["status"]="pass" if result["integrity"]==["ok"] and result["layers"] and all(x.get("crs") and x.get("total",0)>0 and not any(x.get(k,0) for k in ("missing_or_unreadable","empty","invalid")) for x in result["layers"]) else "review"
                except Exception as e:
                    result["status"]="error"
                    result["error"]=str(e)
                report.write(json.dumps(result,default=lambda x:x.tolist() if hasattr(x,"tolist") else str(x))+"\n")
                report.flush()
                print(key,result["status"],flush=True)
