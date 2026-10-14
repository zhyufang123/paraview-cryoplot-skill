#!/usr/bin/env pvpython
# -*- coding: utf-8 -*-
"""
batch_lakesize.py
-----------------
批量处理 ParaView pvtu / vtu 数据，统计每组实验的 lake size（湖泊面积），
并输出：汇总 CSV、逐湖 CSV、每组实验的出图（俯视图 + 可选的 3D 等值面）。

对应技术文档的操作流程：
  读取 vtu/pvtu (Extract Section 等价网格数据)
  -> 按变量阈值提取湖泊 (Select cells + Extract cells 的自动化等价 = Threshold)
  -> 面积随时间变化时用 Connectivity 划分独立湖泊（技术文档重点强调）
  -> Extract surface 提取表面后 Integrate Variables 计算 area
  -> 渲染 + 导出图 / CSV

运行方式（需本机已安装 ParaView，带 pvpython）：
  pvpython batch_lakesize.py
或者把本脚本放入 ParaView/bin 后运行：
  ./pvpython batch_lakesize.py

依赖：本文件同目录下的 lakesize_config.yaml（配置参数，不用改代码）
"""
import os
import sys
import glob
import csv

import yaml

from paraview.simple import *          # noqa: F401,F403  ParaView python API
from paraview import servermanager    # 用于 Fetch 结果表

# --------------------------------------------------------------------------- #
# 0. 读取配置
# --------------------------------------------------------------------------- #
_HERE = os.path.dirname(os.path.abspath(__file__))
_cfg_path = os.path.join(_HERE, "lakesize_config.yaml")
with open(_cfg_path, "r", encoding="utf-8") as _f:
    cfg = yaml.safe_load(_f)

ROOT_DIR      = cfg["root_dir"]          # 所有实验文件夹所在根目录
FIELD_NAME    = cfg["field_name"]        # 用来识别湖泊的变量名
ASSOC         = cfg.get("association", "CELLS")   # CELLS 或 POINTS
LOW_THRESH    = cfg.get("lower_threshold", 0.5)   # 湖泊判据下限
HIGH_THRESH   = cfg.get("upper_threshold", None) # 湖泊判据上限(可为 None)
MODE          = cfg.get("mode", "total")          # total=总湖面积 / per_region=逐湖
USE_EXTRACT_SURFACE = cfg.get("use_extract_surface", True)  # 3D体网格建议 True
OUT_IMAGE_DIR = cfg.get("out_image_dir", os.path.join(ROOT_DIR, "figs"))
SUMMARY_CSV   = cfg.get("summary_csv", os.path.join(ROOT_DIR, "lake_summary.csv"))
REGION_CSV    = cfg.get("region_csv",  os.path.join(ROOT_DIR, "lake_regions.csv"))
FILE_PATTERN  = cfg.get("file_pattern", "*.pvtu")   # 优先 pvtu，找不到再退 vtu
RESOLUTION    = cfg.get("resolution", [1600, 1200]) # 出图分辨率 [宽,高]
COLORMAP      = cfg.get("colormap", "Cool to Warm") # ParaView 色带名
CAMERA_POS    = cfg.get("camera_position", None)    # 固定相机可选 [x,y,z]
CAMERA_FOCAL  = cfg.get("camera_focal_point", None) # 固定焦点可选 [x,y,z]
# --------------------------------------------------------------------------- #

os.makedirs(OUT_IMAGE_DIR, exist_ok=True)


# --------------------------------------------------------------------------- #
# 1. 小工具
# --------------------------------------------------------------------------- #
def find_data_file(exp_dir):
    """在实验目录里找 pvtu 或 vtu，优先 pvtu（pvtu 会自动加载配套 vtu 分片）。"""
    pats = [FILE_PATTERN] if FILE_PATTERN != "*.pvtu" else ["*.pvtu", "*.vtu"]
    for pat in pats:
        hits = sorted(glob.glob(os.path.join(exp_dir, pat)))
        if hits:
            return hits[0]
    # 找不到，兜底搜索所有 vtu
    hits = sorted(glob.glob(os.path.join(exp_dir, "*.vtu")))
    return hits[0] if hits else None


def extract_area(reader, timestep=None):
    """在单个时步上执行 阈值提取湖泊 -> (可选)提取表面 -> 积分算面积。
    返回 total_area (float)；MODE=per_region 时额外返回逐湖面积 dict。
    """
    # 设置当前时步（timestep 为时间序列时使用）
    if timestep is not None:
        reader.UpdatePipeline(time=timestep)

    # ---- Threshold：等价于 "Select cells + Extract cells"，按变量阈值提取湖泊 ----
    thresh = Threshold(Input=reader)
    thresh.Scalars = [ASSOC, FIELD_NAME]
    thresh.LowerThreshold = float(LOW_THRESH)
    if HIGH_THRESH is not None:
        thresh.UpperThreshold = float(HIGH_THRESH)
    thresh.UpdatePipeline()

    # ---- ExtractSurface：3D 体网格需先提表面，面积才有意义（技术文档做法）----
    surface = ExtractSurface(Input=thresh) if USE_EXTRACT_SURFACE else thresh
    surface.UpdatePipeline()

    # ---- Connectivity：划分独立湖泊（技术文档强调面积随时间变化要用它）----
    conn = Connectivity(Input=surface)
    conn.UpdatePipeline()

    # ---- 积分面积 ----
    integrate = IntegrateVariables(Input=conn)
    integrate.UpdatePipeline()
    tbl = servermanager.Fetch(integrate)
    total_area = tbl.GetRowData().GetArray("Area").GetValue(0)

    region_areas = {}
    if MODE == "per_region":
        # 用 connectivity 输出的 RegionId 逐湖提取并积分
        conn_data = servermanager.Fetch(conn)
        arr = conn_data.GetCellData().GetArray("RegionId") \
              if conn_data.GetCellData().GetNumberOfArrays() > 0 else None
        region_ids = []
        if arr is not None:
            seen = set()
            for i in range(arr.GetNumberOfTuples()):
                rid = int(arr.GetValue(i))
                if rid not in seen:
                    seen.add(rid)
                    region_ids.append(rid)
        for rid in region_ids:
            rthresh = Threshold(Input=conn)
            rthresh.Scalars = ["CELLS", "RegionId"]
            rthresh.LowerThreshold = float(rid)
            rthresh.UpperThreshold = float(rid)
            rthresh.UpdatePipeline()
            rint = IntegrateVariables(Input=rthresh)
            rint.UpdatePipeline()
            rtbl = servermanager.Fetch(rint)
            region_areas[rid] = rtbl.GetRowData().GetArray("Area").GetValue(0)

    return total_area, region_areas, thresh


# --------------------------------------------------------------------------- #
# 2. 出图
# --------------------------------------------------------------------------- #
def save_figure(source, exp_name, tag=""):
    """渲染湖泊云图并保存 PNG，遵循 AGU 风格（白底、colorbar、可加比例尺）。"""
    view = CreateRenderView()
    view.Background = [1.0, 1.0, 1.0]
    view.OrientationAxesVisibility = 0  # 关闭坐标轴小人，符合期刊

    disp = Show(source, view)
    disp.SetRepresentationType("Surface")
    ColorBy(disp, (ASSOC, FIELD_NAME))
    lut = GetColorTransferFunction(FIELD_NAME)
    try:
        lut.ApplyPreset(COLORMAP, True)
    except Exception:
        pass
    view.ResetCamera()

    if CAMERA_POS is not None and CAMERA_FOCAL is not None:
        view.CameraPosition = list(CAMERA_POS)
        view.CameraFocalPoint = list(CAMERA_FOCAL)
        view.Update()

    fname = f"{exp_name}.png" if not tag else f"{exp_name}_{tag}.png"
    path = os.path.join(OUT_IMAGE_DIR, fname)
    SaveScreenshot(path, view, ImageResolution=list(RESOLUTION))
    Delete(view)
    return path


# --------------------------------------------------------------------------- #
# 3. 主流程：遍历每组实验
# --------------------------------------------------------------------------- #
def main():
    exp_dirs = [d for d in os.listdir(ROOT_DIR)
                if os.path.isdir(os.path.join(ROOT_DIR, d))]

    # 汇总 CSV 表头
    with open(SUMMARY_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["exp_name", "timestep", "time", "lake_area"])
    if MODE == "per_region":
        with open(REGION_CSV, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f).writerow(["exp_name", "timestep", "region_id", "lake_area"])

    for exp_name in sorted(exp_dirs):
        exp_path = os.path.join(ROOT_DIR, exp_name)
        data_file = find_data_file(exp_path)
        if data_file is None:
            print(f"[skip] {exp_name}: 未找到 pvtu/vtu")
            continue

        # 关键：ResetSession 隔离每组算例，避免 pipeline 残留串扰
        ResetSession()
        reader = XMLUnstructuredGridReader(FileName=[data_file])
        reader.UpdatePipeline()
        print(f"[run] {exp_name}: {os.path.basename(data_file)}")

        # 时间序列：若有多时步，逐时步统计"面积随时间变化"
        times = reader.TimestepValues
        if not times:
            times = [0.0]

        for it, t in enumerate(times):
            try:
                total_area, region_areas, thresh = extract_area(reader, t)
            except Exception as e:
                print(f"[warn] {exp_name} step{it}: 面积计算失败 -> {e}")
                continue

            with open(SUMMARY_CSV, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow([exp_name, it, t, total_area])
            if MODE == "per_region":
                with open(REGION_CSV, "a", newline="", encoding="utf-8-sig") as f:
                    for rid, a in region_areas.items():
                        csv.writer(f).writerow([exp_name, it, rid, a])

            # 出图（仅对当前时步）
            save_figure(thresh, exp_name, tag=f"step{it}")

        print(f"[done] {exp_name}")

    print("\n全部完成：")
    print("  汇总 CSV :", SUMMARY_CSV)
    if MODE == "per_region":
        print("  逐湖 CSV :", REGION_CSV)
    print("  出图目录 :", OUT_IMAGE_DIR)


if __name__ == "__main__":
    main()