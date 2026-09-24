# paraview_plot.py
# Run: pvpython paraview_plot.py
import paraview.simple as pv
import yaml

# Load config
with open("plot_config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

# Read data
reader = pv.NetCDFReader(FileName=[cfg["data_path"]])
reader.Dimensions = cfg["dims"]

# Clip to study region
clip = pv.Clip(Input=reader)
clip.ClipType = "Box"
clip.ClipType.Bounds = cfg["bounds"]

# Color map setup
lut = pv.GetColorTransferFunction(cfg["var_name"])
lut.ApplyPreset(cfg["colormap"], True)

# Render view
renderView = pv.CreateView("RenderView")
renderView.Background = [1.0,1.0,1.0] # white background

# Scale bar & north arrow
scaleBar = pv.ScaleBar()
scaleBar.Input = clip
northArrow = pv.NorthArrow()

# Export vector figure
pv.SaveAnimation(cfg["out_file"], renderView, ImageResolution=cfg["resolution"])
print(f"✅ Figure saved to {cfg['out_file']}")
