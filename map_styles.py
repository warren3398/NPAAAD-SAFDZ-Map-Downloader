"""Shared classification styles without loading plotting libraries."""
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
