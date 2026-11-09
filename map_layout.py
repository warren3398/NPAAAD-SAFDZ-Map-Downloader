"""Offline cartographic layout matching the Slope Map two-panel design."""
from datetime import date
import textwrap
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.gridspec import GridSpec

STYLES={
"npaaad":[("A","All irrigated land/areas",(111,180,102)),("B","Irrigable lands with irrigation projects",(188,224,132)),("C","Alluvial plain lands suitable for agriculture",(255,251,113)),("D","Agro-industrial croplands",(255,178,211)),("E","Highlands suitable for high-value crops",(192,174,138)),("F","Ecologically fragile agricultural lands",(255,192,102)),("G","Fishery areas",(102,132,252)),("H","Forest / watershed areas",(245,245,245)),("BU","Built-up areas",(204,204,204)),("OTHERS","Other land uses",(235,235,235))],
"safdz":[("1","Strategic Crop Sub-Development Zone",(192,254,191)),("2","Strategic Livestock Sub-Development Zone",(182,126,128)),("3","Strategic Fishery Sub-Development Zone",(77,77,255)),("4","Integrated Crop / Livestock Zone",(78,230,77)),("5","Integrated Crop / Fishery Zone",(77,167,77)),("6","Integrated Crop / Livestock / Fishery Zone",(254,212,212)),("7","Integrated Fishery / Livestock Zone",(207,192,77)),("8","Remaining NPAAAD",(255,250,90)),("9","Agro-forestry",(254,197,77)),("10","Watershed / forest areas",(245,245,245)),("BU","Built-up areas",(204,204,204)),("OTHERS","Other land uses",(235,235,235))]
}
def classify(value):
 s=str(value).strip().upper()
 if s in ("BU","BUILT UP","BUILT-UP","BUILTUP"):return "BU"
 if s in ("", "NONE", "NAN"):return "OTHERS"
 return s
def find_field(frame,layer):
 lookup={str(c).upper():c for c in frame.columns if c!="geometry"}
 for key in (layer.upper(),"MAP_SYMBOL","MAPPING_SYMBOL","CLASS","CLASSIFICATION","SYMBOL","CODE","LAND_CLASS","TYPE"):
  if key in lookup:return lookup[key]
 raise ValueError("No classification column identified. Specify --class-field to avoid incorrect legend.")
def render_map(gpkg,layer,province,jpeg_path,pdf_path,class_field=None):
 layer=layer.lower()
 frame=gpd.read_file(gpkg)
 if frame.empty or frame.crs is None:raise ValueError("Missing geometries or CRS")
 field=class_field or find_field(frame,layer)
 if field not in frame:raise ValueError("Classification field not found: "+field)
 frame=frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
 valid={x[0] for x in STYLES[layer]}
 frame["_class"]=frame[field].map(classify)
 frame.loc[~frame["_class"].isin(valid),"_class"]="OTHERS"
 areas=frame.to_crs("EPSG:6933").geometry.area.to_numpy()/10000
 frame["_ha"]=areas
 totals=frame.groupby("_class")["_ha"].sum().to_dict()
 total=sum(totals.values())
 mapframe=frame.to_crs(3857) if frame.crs.is_geographic else frame
 fig=plt.figure(figsize=(16,10),facecolor="white")
 gs=GridSpec(1,2,figure=fig,width_ratios=[2.55,1],wspace=.035,left=.02,right=.98,top=.965,bottom=.035)
 ax=fig.add_subplot(gs[0,0]);side=fig.add_subplot(gs[0,1]);side.set_xlim(0,1);side.set_ylim(0,1);side.axis("off")
 ax.set_facecolor("#eaf0f2")
 for code,description,rgb in STYLES[layer]:
  sub=mapframe.loc[frame["_class"]==code]
  if not sub.empty:sub.plot(ax=ax,color=tuple(v/255 for v in rgb),edgecolor="none",linewidth=0)
 ax.set_aspect("equal");ax.set_xticks([]);ax.set_yticks([])
 ax.annotate("N",xy=(.96,.88),xytext=(.96,.95),xycoords="axes fraction",textcoords="axes fraction",ha="center",fontsize=17,fontweight="bold",arrowprops=dict(arrowstyle="-|>",color="#17402d"))
 for spine in ax.spines.values():spine.set_color("#17402d")
 def heading(y,text):
  side.add_patch(Rectangle((0,y-.043),1,.043,facecolor="#07542d",transform=side.transAxes))
  side.text(.5,y-.0215,text,ha="center",va="center",color="white",weight="bold",fontsize=12,transform=side.transAxes)
 heading(1,layer.upper()+" MAP")
 side.text(.5,.925,province.replace("-"," ").title(),ha="center",va="center",fontsize=14,weight="bold",transform=side.transAxes)
 heading(.875,"Legend")
 for i,(code,description,rgb) in enumerate(STYLES[layer]):
  y=.803-i*.025
  side.add_patch(Rectangle((.035,y-.008),.06,.018,facecolor=tuple(v/255 for v in rgb),edgecolor="#999",linewidth=.3,transform=side.transAxes))
  side.text(.115,y,code,fontsize=8.5,weight="bold",va="center",transform=side.transAxes)
  side.text(.22,y,description,fontsize=7.2,va="center",transform=side.transAxes)
 heading(.485,"Area Summary")
 side.text(.04,.416,"Class",weight="bold",fontsize=9,transform=side.transAxes)
 side.text(.74,.416,"Area (ha)",ha="right",weight="bold",fontsize=9,transform=side.transAxes)
 side.text(.97,.416,"Area (%)",ha="right",weight="bold",fontsize=9,transform=side.transAxes)
 for i,(code,_,_) in enumerate(STYLES[layer]):
  y=.387-i*.023
  ha=totals.get(code,0)
  side.text(.04,y,code,fontsize=8,transform=side.transAxes)
  side.text(.74,y,f"{ha:,.2f}",ha="right",fontsize=8,transform=side.transAxes)
  side.text(.97,y,f"{100*ha/total if total else 0:.2f}",ha="right",fontsize=8,transform=side.transAxes)
 y=.387-len(STYLES[layer])*.023
 side.text(.04,y,"TOTAL",fontsize=8.5,weight="bold",transform=side.transAxes)
 side.text(.74,y,f"{total:,.2f}",ha="right",fontsize=8.5,weight="bold",transform=side.transAxes)
 side.text(.97,y,"100.00" if total else "0.00",ha="right",fontsize=8.5,weight="bold",transform=side.transAxes)
 heading(.116,"Map Information")
 side.text(.04,.055,"Source: NPAAAD / SAFDZ GeoPackage",fontsize=8,transform=side.transAxes)
 side.text(.04,.035,"CRS: "+str(mapframe.crs),fontsize=8,transform=side.transAxes)
 side.text(.04,.015,"Generated: "+date.today().isoformat(),fontsize=8,transform=side.transAxes)
 fig.savefig(jpeg_path,dpi=170,facecolor="white",pil_kwargs={"quality":92})
 fig.savefig(pdf_path,facecolor="white")
 plt.close(fig)
 return {"feature_count":len(frame),"total_ha":total,"class_field":field}
